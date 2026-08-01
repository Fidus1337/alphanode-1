"""Forward track — append-only paper stepping of enrolled strategies INSIDE the node.

Enrolling FREEZES a strategy (formulas + universe + vol/fee) as of that day; from then on the
node steps it once per closed daily bar on LIVE Binance data with the same semantics as a
paper-trade bundle: run the real engine up to the last closed bar -> target weights ->
mark-to-market -> rebalance -> fees -> log. The account lives in the entry, one JSON per node
(state/forward.json). History is APPEND-ONLY: forward numbers are written by live steps and
never recomputed backwards — a "recomputed" track would just be another backtest.

CLI (the GUI spawns this as a worker; cron users can call it directly):
    python alphanode/forward_track.py step [--force]   # step every due entry
    python alphanode/forward_track.py list             # one line per entry

Daily timeframe only — the stepping engine is the real quantpylib Portfolio (same as paper).
"""
import os
import sys
import json
import time
import hashlib
import argparse
import warnings
import urllib.request
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
for _p in (os.path.join(PROJ, 'evolution'), PROJ, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np                                        # noqa: E402
import pandas as pd                                       # noqa: E402

warnings.filterwarnings('ignore')
np.seterr(all='ignore')

DUST = 1.0                                                # ignore rebalances under $1 notional
KLINES = 'https://fapi.binance.com/fapi/v1/klines'
START_CAPITAL = 10000.0


def _state_dir():
    d = os.environ.get('ALPHANODE_STATE_DIR') or os.path.join(HERE, 'state')
    os.makedirs(d, exist_ok=True)
    return d


def track_file():
    return os.path.join(_state_dir(), 'forward.json')


def load_track():
    try:
        with open(track_file(), encoding='utf-8') as f:
            t = json.load(f)
        t.setdefault('entries', [])
        return t
    except (OSError, json.JSONDecodeError):
        return {'entries': []}


def save_track(track):
    path = track_file()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(track, f, indent=1)
    os.replace(tmp, path)


def new_entry(name, kind, formulas, tickers, vol, exec_rate, engine_start,
              capital=START_CAPITAL):
    """A frozen strategy: everything a step needs, snapshotted at enrollment."""
    sig = hashlib.md5(('|'.join(formulas) + '#' + ','.join(sorted(tickers))).encode()).hexdigest()[:6]
    return {
        'id': f'{name}_{sig}',
        'name': name, 'kind': kind,                      # 'alpha' | 'portfolio'
        'formulas': list(formulas), 'tickers': list(tickers),
        'vol': float(vol), 'exec': float(exec_rate),
        'engine_start': str(engine_start)[:10],          # warm-up start for the simulation
        'start_capital': float(capital),
        'enrolled': datetime.now(timezone.utc).date().isoformat(),
        'archived': False,
        'state': {'equity': float(capital), 'positions': {}, 'prices': {}, 'last_run': None},
        'history': [],
    }


def find_duplicate(track, formulas, tickers):
    """An ACTIVE entry with the same frozen strategy (formulas+universe)."""
    key = ('|'.join(formulas), ','.join(sorted(tickers)))
    for e in track['entries']:
        if not e.get('archived') and (('|'.join(e['formulas']), ','.join(sorted(e['tickers']))) == key):
            return e
    return None


def metrics(entry):
    """days / total return / annualized Sharpe / max drawdown from the append-only history."""
    hist = entry.get('history') or []
    eq = [entry['start_capital']] + [h['equity'] for h in hist]
    out = {'days': len(hist), 'equity': eq[-1], 'ret': eq[-1] / eq[0] - 1.0,
           'sharpe': None, 'dd': None, 'last': (hist[-1]['date'] if hist else None)}
    if len(eq) >= 3:
        e = np.asarray(eq, dtype=float)
        r = e[1:] / e[:-1] - 1.0
        peak = np.maximum.accumulate(e)
        out['dd'] = float((e / peak - 1.0).min())
        if len(r) >= 10 and r.std() > 0:
            out['sharpe'] = float(r.mean() / r.std() * np.sqrt(365.0))
    return out


# ---- live data (public USD-M klines; no keys) ----
def _now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _fetch_json(url, retries=4):
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError):
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def fetch_klines(symbol, start_ms, end_ms):
    out, cur = [], start_ms
    while cur < end_ms:
        url = f'{KLINES}?symbol={symbol}&interval=1d&startTime={cur}&endTime={end_ms}&limit=1500'
        data = _fetch_json(url)
        if not data:
            break
        out.extend(data)
        if len(data) < 1500:
            break
        cur = data[-1][0] + 1
        time.sleep(0.1)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out, columns=['openTime', 'open', 'high', 'low', 'close', 'volume',
                                    'closeTime', 'qav', 'trades', 'tbb', 'tbq', 'ig'])
    df = df[df['closeTime'] <= _now_ms()]                 # CLOSED candles only — no live bar
    for c in ('open', 'high', 'low', 'close', 'volume'):
        df[c] = df[c].astype(float)
    df['datetime'] = pd.to_datetime(df['openTime'], unit='ms', utc=True)
    df = df.set_index('datetime')[['open', 'high', 'low', 'close', 'volume']]
    return df[~df.index.duplicated()]


def _compute_targets(entry, tickers, dfs, end):
    """Same maths as the paper bundle: each formula via the real engine, combined by Portfolio;
    the last row gives target weights + leverage (one alpha = a one-strategy portfolio)."""
    from evolved_strategy import make_evolved
    from quantpylib.simulator.alpha import Portfolio
    start = datetime.fromisoformat(entry['engine_start'])
    stratdfs = []
    for i, formula in enumerate(entry['formulas']):
        Alpha = make_evolved(formula, f'F{i}')
        a = Alpha(insts=tickers, dfs={t: dfs[t].copy() for t in tickers}, start=start, end=end,
                  portfolio_vol=entry['vol'], execrates=entry['exec'])
        stratdfs.append(a.run_simulation())
    pf = Portfolio(insts=tickers, dfs={t: dfs[t].copy() for t in tickers}, start=start, end=end,
                   stratdfs=stratdfs, portfolio_vol=entry['vol'], execrates=entry['exec'])
    last = pf.run_simulation().iloc[-1]
    lev = float(last.get('leverage', 0.0))
    weights = {t: float(last.get(f'{t} w', 0.0)) for t in tickers}
    return weights, lev


def step_entry(entry, kline_cache, force=False, log=print):
    """One trading step for one entry. Returns True if the entry advanced (needs saving)."""
    ok, dfs = [], {}
    start_ms = int(pd.Timestamp(entry['engine_start'], tz='UTC').timestamp() * 1000)
    for t in entry['tickers']:
        if t not in kline_cache:
            try:
                kline_cache[t] = fetch_klines(t, start_ms, _now_ms())
            except Exception as e:                        # noqa: BLE001
                kline_cache[t] = pd.DataFrame()
                log(f'  {t}: download failed ({type(e).__name__})')
        df = kline_cache[t]
        if len(df) > 60:
            dfs[t] = df
            ok.append(t)
    if not ok:
        log(f'[{entry["id"]}] no data for any ticker — step skipped')
        return False
    tickers = ok
    last_date = max(df.index[-1] for df in dfs.values())
    end = datetime(last_date.year, last_date.month, last_date.day)
    end_str = f'{end:%Y-%m-%d}'
    st = entry['state']
    if not force and st.get('last_run') == end_str:
        log(f'[{entry["id"]}] up to date ({end_str})')
        return False

    log(f'[{entry["id"]}] stepping to {end_str} ({len(tickers)} assets)…')
    weights, lev = _compute_targets(entry, tickers, dfs, end)
    prices = {t: float(dfs[t]['close'].iloc[-1]) for t in tickers}

    equity = float(st['equity'])
    positions = {t: float(v) for t, v in (st.get('positions') or {}).items()}
    prev_prices = st.get('prices') or {}
    pnl = sum(positions.get(t, 0.0) * (prices[t] - float(prev_prices.get(t, prices[t])))
              for t in tickers)
    equity += pnl
    target = {t: (weights.get(t, 0.0) * lev * equity / prices[t]) if prices[t] > 0 else 0.0
              for t in tickers}
    turnover = sum(abs((target[t] - positions.get(t, 0.0)) * prices[t]) for t in tickers
                   if abs((target[t] - positions.get(t, 0.0)) * prices[t]) > DUST)
    fees = turnover * entry['exec']
    equity -= fees

    st['positions'] = {t: target[t] for t in tickers if abs(target[t] * prices[t]) > DUST}
    st['prices'] = prices
    st['equity'] = equity
    st['last_run'] = end_str
    row = {'date': end_str, 'equity': round(equity, 2), 'pnl': round(pnl, 2),
           'fees': round(fees, 2), 'turnover': round(turnover, 2), 'leverage': round(lev, 3)}
    hist = entry['history']
    if hist and hist[-1]['date'] == end_str:              # a force re-step overwrites the same bar
        hist[-1] = row
    else:
        hist.append(row)
    log(f'  equity ${equity:,.2f} · P&L ${pnl:+,.2f} · fees ${fees:,.2f} · lev {lev:.2f}')
    return True


def step_all(force=False, log=print):
    track = load_track()
    active = [e for e in track['entries'] if not e.get('archived')]
    if not active:
        log('forward track is empty — enroll a champion or a portfolio in the GUI')
        return 0
    cache = {}
    stepped = 0
    for e in active:
        try:
            if step_entry(e, cache, force=force, log=log):
                stepped += 1
                save_track(track)                        # crash-safe: persist after every entry
        except Exception as ex:                          # noqa: BLE001
            log(f'[{e["id"]}] step failed: {type(ex).__name__}: {ex}')
    log(f'done: {stepped}/{len(active)} entries advanced')
    return 0


def main():
    ap = argparse.ArgumentParser(description='Forward track: append-only paper steps of enrolled strategies')
    ap.add_argument('cmd', choices=('step', 'list'))
    ap.add_argument('--force', action='store_true', help='re-step even if the bar was processed')
    args = ap.parse_args()
    if args.cmd == 'list':
        track = load_track()
        for e in track['entries']:
            m = metrics(e)
            sh = f'{m["sharpe"]:+.2f}' if m['sharpe'] is not None else '—'
            print(f'{"[arch] " if e.get("archived") else ""}{e["id"]}: {m["days"]}d · '
                  f'${m["equity"]:,.0f} ({m["ret"]*100:+.1f}%) · Sharpe {sh} · since {e["enrolled"]}')
        return 0
    return step_all(force=args.force)


if __name__ == '__main__':
    sys.exit(main())
