"""Build a combined PORTFOLIO from the top-N alphas by fitness base = min(TRAIN, VAL) Sharpe,
using the project's real `Portfolio` engine (quantpylib), and write metrics + equity to JSON for
the GUI panel.

Selection deliberately does NOT look at TEST (it used to sort by TEST Sharpe — a held-out peek
that made the combined TEST number a self-fulfilling cherry-pick). With selection on base, the
reported TEST metrics of the combined book are a genuine out-of-sample evaluation.

The per-alpha simulations are run in parallel processes (the real engine loop is slow); the
combined book is then produced by the real Portfolio object.

    python alphanode/portfolio_build.py --top 6 --out state/portfolio.json
"""
import os
import sys
import json
import time
import difflib
import argparse
import warnings
import multiprocessing as mp

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
for _p in (os.path.join(PROJ, 'evolution'), PROJ, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np                                        # noqa: E402
import pandas as pd                                       # noqa: E402
warnings.filterwarnings('ignore'); np.seterr(all='ignore')


def _state_dir():
    return os.environ.get('ALPHANODE_STATE_DIR') or os.path.join(HERE, 'state')


def _basesh(c):
    """Selection key: base = min(train,val) Sharpe — same fitness the search optimized.
    TEST stays out of selection (held-out evaluation only)."""
    b = c.get('base')
    if b is None:
        tr = (c.get('train') or {}).get('sharpe')
        va = (c.get('val') or {}).get('sharpe')
        b = min(tr, va) if (tr is not None and va is not None) else None
    return b


def _pick_top(n):
    """Top-N alphas by base=min(train,val) from the library (diverse, no near-clones)."""
    lib = os.path.join(_state_dir(), 'library.jsonl')
    rows = []
    for line in open(lib, encoding='utf-8'):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    rows = [c for c in rows if _basesh(c) is not None]
    rows.sort(key=_basesh, reverse=True)
    kept, top = [], []
    for c in rows[:500]:
        f = c['formula']
        if all(difflib.SequenceMatcher(None, f, k).ratio() < 0.85 for k in kept):
            kept.append(f); top.append(c)
        if len(top) >= n:
            break
    return top


# ---- worker: simulate one formula on the REAL engine, return the columns Portfolio needs ----
_G = {}


def _winit(sim_start, sim_end):
    from config import load_config
    from evaluator import load_raw
    cfg = load_config()
    # workers only run the engine on the RAW dfs — skip build_panel's wide feature tables
    # (11 x N x days), which _sim_one never touches, to save per-worker memory + startup CPU.
    tk, raw = load_raw(cfg['data'], cfg.get('instruments'))
    try:
        os.nice(10)                                       # background priority: keep the GUI responsive
    except (AttributeError, OSError):
        pass
    _G.update(cfg=cfg, tk=tk, raw=raw, start=sim_start, end=sim_end)


def _sim_one(arg):
    i, formula = arg
    from evolved_strategy import make_evolved
    tk, raw = _G['tk'], _G['raw']
    Strat = make_evolved(formula, f'S{i}')
    a = Strat(insts=tk, dfs={t: raw[t].copy() for t in tk}, start=_G['start'], end=_G['end'],
              portfolio_vol=_G['cfg']['vol'], execrates=_G['cfg']['exec'])
    sdf = a.run_simulation()
    keep = [f'{t} w' for t in tk] + ['leverage', 'capital_ret']
    return i, sdf[keep]


def _metrics(capital_ret, lo, hi):
    r = capital_ret[(capital_ret.index >= lo) & (capital_ret.index < hi)].dropna()
    if len(r) < 5 or r.std() == 0:
        return None
    eq = (1 + r).cumprod()
    return {'sharpe': float((r.mean() / r.std()) * np.sqrt(365)),
            'cagr': float(eq.iloc[-1] ** (365 / len(r)) - 1),
            'dd': float((eq / eq.cummax() - 1).min()), 'n': int(len(r))}


def build(top_n, sim_start, jobs, out_path):
    from config import load_config
    from evaluator import build_panel, basket_returns
    from quantpylib.simulator.alpha import Portfolio

    t0 = time.time()
    cfg = load_config()
    tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'))
    ts, te = cfg['splits']['test']
    start = pd.Timestamp(sim_start, tz='UTC').tz_localize(None).to_pydatetime()
    end = te.tz_localize(None).to_pydatetime()

    top = _pick_top(top_n)
    if len(top) < 2:
        raise RuntimeError('need at least 2 alphas with a TEST score in the library')
    formulas = [c['formula'] for c in top]
    print(f'· combining top-{len(formulas)} by base=min(train,val) — TEST stays held out '
          f'(real engine, {jobs} workers)…', flush=True)

    items = list(enumerate(formulas))
    results = {}
    with mp.Pool(processes=jobs, initializer=_winit, initargs=(start, end)) as pool:
        for done, (i, sdf) in enumerate(pool.imap_unordered(_sim_one, items), 1):
            results[i] = sdf
            print(f'  [{done}/{len(items)}] strategy S{i} simulated', flush=True)
    stratdfs = [results[i] for i in range(len(formulas))]

    print('· running the Portfolio combiner…', flush=True)
    pf = Portfolio(stratdfs=stratdfs, insts=tk, dfs={t: raw[t].copy() for t in tk},
                   start=start, end=end, portfolio_vol=cfg['vol'], execrates=cfg['exec'])
    comb = pf.run_simulation()

    m = _metrics(comb['capital_ret'], ts, te)
    indiv = [(_metrics(sdf['capital_ret'], ts, te) or {}).get('sharpe') for sdf in stratdfs]
    bh = basket_returns(panel)
    bh_m = _metrics(bh, ts, te)

    # equity on TEST (combined + basket), lightly downsampled for the GUI
    cr = comb['capital_ret']; cr = cr[(cr.index >= ts) & (cr.index < te)].fillna(0.0)
    ce = (1 + cr).cumprod()
    br = bh[(bh.index >= ts) & (bh.index < te)].fillna(0.0); be = (1 + br).cumprod()
    dates = [d.strftime('%Y-%m-%d') for d in ce.index]

    # combined target weights over TEST (for the "Download signals" CSV / paper-trade in the GUI)
    present = [t for t in tk if f'{t} w' in comb.columns]
    cw = comb.loc[(comb.index >= ts) & (comb.index < te), [f'{t} w' for t in present]]
    cw = cw[cw.abs().sum(axis=1) > 0]                     # drop empty days
    weights = {'dates': [d.strftime('%Y-%m-%d') for d in cw.index], 'tickers': present,
               'W': [[round(float(x), 5) for x in row] for row in cw.to_numpy()]}

    doc = {'ok': True, 'n': len(formulas), 'sel': 'base',   # selection key; docs without 'sel'
           'sim_start': str(pd.Timestamp(start).date()),    # were built by the old TEST-sorted picker
           'test': f'{ts.date()}..{te.date()}',
           'metrics': m, 'basket': bh_m, 'indiv_sharpe': indiv,
           'formulas': [f[:90] for f in formulas], 'formulas_full': formulas,
           'weights': weights,
           'equity': {'dates': dates, 'combined': [round(float(x), 5) for x in ce.values],
                      'basket': [round(float(x), 5) for x in be.values]},
           'built_secs': round(time.time() - t0, 1)}
    tmp = out_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f)
    os.replace(tmp, out_path)
    sh = m['sharpe'] if m else float('nan')
    print(f'✓ portfolio built: Sharpe {sh:+.2f} · {doc["built_secs"]}s → {out_path}', flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description='Build a combined Portfolio from top-N alphas by base=min(train,val)')
    ap.add_argument('--top', type=int, default=6)
    ap.add_argument('--sim-start', default='2022-06-01', help='warm-up start before TEST (speed)')
    ap.add_argument('--jobs', type=int, default=0, help='parallel workers (0 = auto)')
    ap.add_argument('--out', default=os.path.join(_state_dir(), 'portfolio.json'))
    args = ap.parse_args()
    jobs = args.jobs if args.jobs > 0 else max(1, min(args.top, (os.cpu_count() or 4) - 2))
    try:
        rc = build(args.top, args.sim_start, jobs, args.out)
    except Exception as e:                                 # noqa: BLE001
        print(f'✗ portfolio build failed: {type(e).__name__}: {e}', flush=True)
        try:
            with open(args.out, 'w', encoding='utf-8') as f:
                json.dump({'ok': False, 'error': f'{type(e).__name__}: {e}'}, f)
        except OSError:
            pass
        rc = 1
    sys.exit(rc)


if __name__ == '__main__':
    main()
