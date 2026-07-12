"""Table of the champion portfolio's asset entries on the TEST sample.

Portfolio = an ensemble of selected champions (blended via quantpylib.Portfolio) or a single
champion (--rank). An "entry" = the day an asset's position CHANGES SIGN (opening or a
long<->short flip). We show only events inside TEST plus the starting snapshot
(what was carried into TEST from VAL). It is computed through the REAL engine (asset-level
positions are needed), so an ensemble of several champions takes a couple of minutes.

  python champion_entries.py                 # ensemble of the top-4 by base
  python champion_entries.py --by test --top 6
  python champion_entries.py --rank 6        # a single champion #6
  python champion_entries.py --all           # all champions from champions.json
"""
import os
import sys
import csv
import json
import argparse
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
np.seterr(divide='ignore', invalid='ignore')

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from config import load_config                          # noqa: E402
from evaluator import build_panel                       # noqa: E402
from evolved_strategy import make_evolved               # noqa: E402
from quantpylib.simulator.alpha import Portfolio        # noqa: E402


def fresh(raw, tk):
    return {t: raw[t].copy() for t in tk}


def select(champs, args):
    if args.all:
        return champs
    if args.rank is not None:
        return [c for c in champs if c['rank'] == args.rank]
    if args.by == 'test':
        pool = sorted([c for c in champs if c.get('test')], key=lambda c: -c['test']['sharpe'])
    else:
        pool = sorted(champs, key=lambda c: -c['base'])
    return pool[:args.top]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rank', type=int, help='a single champion by rank')
    ap.add_argument('--top', type=int, default=4, help='how many champions in the ensemble')
    ap.add_argument('--by', choices=['base', 'test'], default='base', help='what to rank the selection by')
    ap.add_argument('--all', action='store_true', help='take all champions')
    ap.add_argument('--max-rows', type=int, default=40, help='how many entry rows to print')
    ap.add_argument('--min-weight', type=float, default=0.5,
                    help='weight threshold %% for an "entry" — cut off dust (flips near zero)')
    args = ap.parse_args()

    cfg = load_config()
    champs = json.load(open(os.path.join(HERE, 'champions.json')))['champions']
    sel = select(champs, args)
    if not sel:
        print('No champions selected.')
        return

    tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'))
    s = panel['close'].index[0].to_pydatetime().replace(tzinfo=None)
    e = panel['close'].index[-1].to_pydatetime().replace(tzinfo=None)
    te0, te1 = cfg['splits']['test']

    print('Portfolio of champions:')
    for c in sel:
        te = c['test']['sharpe'] if c.get('test') else float('nan')
        print(f'  #{c["rank"]:>2}  base {c["base"]:+.2f}  TEST {te:+.2f}  {c["formula"][:58]}')
    print(f'Running through the engine ({len(sel)} pcs)... this will take ~{max(1, len(sel))}×0.5 min.')

    subs = []
    for c in sel:
        Cls = make_evolved(c['formula'], f'C{c["rank"]}')
        a = Cls(insts=tk, dfs=fresh(raw, tk), start=s, end=e,
                portfolio_vol=cfg['vol'], execrates=cfg['exec'])
        subs.append(a.run_simulation())
    if len(subs) == 1:
        port = subs[0]
    else:
        p = Portfolio(insts=tk, dfs=fresh(raw, tk), start=s, end=e,
                      stratdfs=subs, portfolio_vol=cfg['vol'], execrates=cfg['exec'])
        port = p.run_simulation()

    lev, cap = port['leverage'].fillna(0.0), port['capital'].fillna(0.0)
    in_test = (port.index >= te0) & (port.index < te1)

    # ---- starting snapshot: what was carried into TEST from VAL ----
    first = port.index[in_test][0]
    snap = []
    for inst in tk:
        u = port.at[first, f'{inst} units'] if f'{inst} units' in port else 0.0
        w = port.at[first, f'{inst} w'] if f'{inst} w' in port else 0.0
        if u and pd.notna(w) and abs(w) > 0:
            snap.append((inst, 'LONG' if u > 0 else 'SHORT', abs(w) * 100,
                         abs(w * lev.at[first] * cap.at[first])))
    snap.sort(key=lambda x: -x[3])
    print(f'\nStarting portfolio snapshot at entry into TEST ({first.date()}) — {len(snap)} assets:')
    for inst, side, wp, notion in snap[:12]:
        print(f'    {inst:10s} {side:5s}  weight {wp:5.1f}%   ${notion:>10,.0f}')
    if len(snap) > 12:
        print(f'    ... {len(snap) - 12} more')

    # ---- trades = a "run" of a single sign inside TEST, with PnL until the reversal ----
    # asset PnL for a day = units[yesterday] * (close[today] - close[yesterday]) — as in the engine.
    close = panel['close']
    trades = []
    for inst in tk:
        ucol = f'{inst} units'
        if ucol not in port:
            continue
        u = port[ucol].fillna(0.0)
        wv = port[f'{inst} w'].fillna(0.0).to_numpy()
        c = close[inst].reindex(port.index)
        pnl_series = (u.shift(1) * c.diff()).fillna(0.0).to_numpy()
        uv = u.to_numpy()
        s = np.sign(uv)
        idx = port.index
        n = len(s)
        i = 0
        while i < n:
            if s[i] == 0:
                i += 1
                continue
            j = i
            while j + 1 < n and s[j + 1] == s[i]:      # extend the run of a single sign
                j += 1
            entry_date = idx[i]
            if te0 <= entry_date < te1:                # the entry happened inside TEST
                peak_w = float(np.abs(wv[i:j + 1]).max() * 100)
                if peak_w >= args.min_weight:          # the position became significant -> it's a trade
                    hi = min(j + 1, n - 1)             # earns through the reversal day inclusive
                    pnl = float(pnl_series[i + 1:hi + 1].sum()) if i + 1 <= hi else 0.0
                    cap_e = cap.iloc[i]
                    trades.append({
                        'entry': str(entry_date.date()), 'exit': str(idx[hi].date()),
                        'asset': inst, 'side': 'LONG' if s[i] > 0 else 'SHORT',
                        'peak_w%': round(peak_w, 2), 'days': int(j - i + 1),
                        'pnl_usd': round(pnl, 2),
                        'pnl_book%': round(pnl / cap_e * 100, 3) if cap_e else 0.0})
            i = j + 1
    trades.sort(key=lambda t: t['entry'])

    out = os.path.join(HERE, 'entries_test.csv')
    with open(out, 'w', newline='') as f:
        wtr = csv.DictWriter(f, fieldnames=['entry', 'exit', 'asset', 'side', 'peak_w%',
                                            'days', 'pnl_usd', 'pnl_book%'])
        wtr.writeheader()
        wtr.writerows(trades)

    wins = sum(1 for t in trades if t['pnl_usd'] > 0)
    tot = sum(t['pnl_usd'] for t in trades)
    print(f'\nTrades (peak weight ≥{args.min_weight}%) on TEST: {len(trades)} | '
          f'profitable {wins}/{len(trades)} ({100 * wins / max(1, len(trades)):.0f}%) | '
          f'total PnL ${tot:,.0f}')
    print(f'\n{"entry":12s}{"asset":10s}{"side":6s}{"peak%":>7s}{"days":>6s}{"PnL $":>15s}{"%book":>9s}')
    print('-' * 65)
    for t in trades[:args.max_rows]:
        print(f'{t["entry"]:12s}{t["asset"]:10s}{t["side"]:6s}{t["peak_w%"]:>7.2f}'
              f'{t["days"]:>6d}{t["pnl_usd"]:>15,.0f}{t["pnl_book%"]:>+8.2f}%')
    if len(trades) > args.max_rows:
        print(f'... {len(trades) - args.max_rows} more (all in CSV)')

    # ---- best / worst entries ----
    by_pnl = sorted(trades, key=lambda t: -t['pnl_usd'])
    print('\nTop-5 profitable:')
    for t in by_pnl[:5]:
        print(f'    {t["entry"]}  {t["asset"]:9s} {t["side"]:5s} peak {t["peak_w%"]:5.1f}%  '
              f'${t["pnl_usd"]:>14,.0f}  ({t["pnl_book%"]:+.2f}% book)')
    print('Top-5 losing:')
    for t in by_pnl[-5:][::-1]:
        print(f'    {t["entry"]}  {t["asset"]:9s} {t["side"]:5s} peak {t["peak_w%"]:5.1f}%  '
              f'${t["pnl_usd"]:>14,.0f}  ({t["pnl_book%"]:+.2f}% book)')

    # ---- per-asset summary (sorted by PnL) ----
    pnl_a, n_a = {}, {}
    for t in trades:
        pnl_a[t['asset']] = pnl_a.get(t['asset'], 0.0) + t['pnl_usd']
        n_a[t['asset']] = n_a.get(t['asset'], 0) + 1
    test = port[in_test]
    summ = []
    for inst in tk:
        col = f'{inst} units'
        if col not in test:
            continue
        u = test[col].fillna(0.0)
        longd, shortd = int((u > 0).sum()), int((u < 0).sum())
        if longd + shortd == 0 and inst not in pnl_a:
            continue
        summ.append((inst, longd, shortd, n_a.get(inst, 0), pnl_a.get(inst, 0.0)))
    summ.sort(key=lambda x: -x[4])
    print('\nPer-asset summary on TEST (by PnL):')
    print(f'{"asset":10s}{"d.LONG":>9s}{"d.SHORT":>10s}{"trades":>8s}{"PnL $":>16s}')
    print('-' * 53)
    for inst, longd, shortd, nt, pnl in summ:
        print(f'{inst:10s}{longd:>9d}{shortd:>10d}{nt:>8d}{pnl:>16,.0f}')

    print(f'\nFull trade table with PnL: {out}')


if __name__ == '__main__':
    main()
