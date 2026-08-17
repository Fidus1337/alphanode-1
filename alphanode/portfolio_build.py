"""Build a combined PORTFOLIO from the top-N library alphas and write metrics + equity to JSON
for the GUI panel.

Two engines, chosen by the active timeframe:
  1d       — the project's real `Portfolio` engine (quantpylib): matches paper/Serve bar-for-bar.
  intraday — evolution's fastsim, run TWICE: once per member alpha to record each bar's
             weight×leverage path, then once more over the SUM of those paths. That sum is
             literally what quantpylib's Portfolio.compute_forecasts feeds its own loop, so the
             combination semantics (second vol-targeting + inertia layer) are reproduced in the
             engine whose numbers the intraday search leaderboard shows.

Two selection modes (--select):
  test  (default) — top-N by held-out TEST Sharpe: picks what actually worked on the recent
        out-of-sample window. ⚠ The combined TEST metrics are then OPTIMISTIC (the same window
        picked the members — a cherry-pick); treat them as a shortlist, validate forward/paper.
  base  — top-N by fitness min(TRAIN, VAL) Sharpe: TEST never enters selection, so the combined
        TEST metrics are a genuine out-of-sample evaluation.

The per-alpha simulations are run in parallel processes (the real engine loop is slow); the
combined book is then produced by the real Portfolio object.

    python alphanode/portfolio_build.py --top 6 --select test --out state/portfolio.json
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
    d = os.environ.get('ALPHANODE_STATE_DIR')
    if d:
        return d
    import apppaths                                # not HERE/state: frozen, that's the read-only bundle
    return apppaths.state_dir()


def _basesh(c):
    """Fitness key: base = min(train,val) Sharpe — the same number the search optimized."""
    b = c.get('base')
    if b is None:
        tr = (c.get('train') or {}).get('sharpe')
        va = (c.get('val') or {}).get('sharpe')
        b = min(tr, va) if (tr is not None and va is not None) else None
    return b


def _testsh(c):
    """Held-out TEST Sharpe (selection by it = cherry-pick; see the module docstring)."""
    t = c.get('test') if isinstance(c.get('test'), dict) else {}
    return t.get('sharpe')


def _pick_top(n, select='test', lib=None):
    """Top-N alphas by the chosen key from the library (diverse, no near-clones)."""
    key = _basesh if select == 'base' else _testsh
    lib = lib or os.path.join(_state_dir(), 'library.jsonl')
    rows = []
    for line in open(lib, encoding='utf-8'):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    rows = [c for c in rows if key(c) is not None and c.get('formula')]   # sealed rows (vault)
    #                                    carry full metrics but no plaintext — nothing to simulate
    rows.sort(key=key, reverse=True)
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


def _metrics(capital_ret, lo, hi, ann=365):
    r = capital_ret[(capital_ret.index >= lo) & (capital_ret.index < hi)].dropna()
    if len(r) < 5 or r.std() == 0:
        return None
    eq = (1 + r).cumprod()
    return {'sharpe': float((r.mean() / r.std()) * np.sqrt(ann)),
            'cagr': float(eq.iloc[-1] ** (ann / len(r)) - 1),
            'dd': float((eq / eq.cummax() - 1).min()), 'n': int(len(r))}


def _seg_metrics(ret, splits, ann=365):
    """Per-segment metrics of a returns series: {'train': {...}, 'val': {...}, 'test': {...}}.
    A segment the simulation does not cover (late --sim-start) comes back None."""
    return {name: _metrics(ret, splits[name][0], splits[name][1], ann)
            for name in ('train', 'val', 'test')}


def _bounds(splits):
    """Segment boundary dates for the GUI chart (vertical markers + labels)."""
    return {'train_start': str(splits['train'][0].date()), 'val_start': str(splits['val'][0].date()),
            'test_start': str(splits['test'][0].date()), 'test_end': str(splits['test'][1].date())}


def _seg_line(segs):
    return ' / '.join(f'{n.upper()} ' + (f'{m["sharpe"]:+.2f}' if m else '—')
                      for n, m in segs.items())


def _write_doc(doc, out_path):
    tmp = out_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f)
    os.replace(tmp, out_path)


def _apply_segments(cfg):
    """Segment dates, node.py-style: ALPHANODE_{TRAIN,VAL,TEST}_START/TEST_END env (the GUI's
    date fields) wins; otherwise an intraday tf falls back to its recommended windows — the
    ini's [segments] are the DAILY defaults and would mislabel intraday TEST."""
    names = ('TRAIN_START', 'VAL_START', 'TEST_START', 'TEST_END')
    raw = [os.environ.get('ALPHANODE_' + n) or None for n in names]
    if not any(raw) and cfg.get('tf', '1d') != '1d':
        from timeframe import resolve
        seg = resolve(cfg['tf']).segments
        raw = [seg['train_start'], seg['val_start'], seg['test_start'], seg['test_end']]
    if not any(raw):
        return
    sp = cfg['splits']
    cur = [sp['train'][0], sp['val'][0], sp['test'][0], sp['test'][1]]
    tr, va, te, en = [pd.Timestamp(r, tz='UTC') if r else cur[i] for i, r in enumerate(raw)]
    cfg['splits'] = {'train': (tr, va), 'val': (va, te), 'test': (te, en)}
    cfg['start'] = tr.tz_localize(None).to_pydatetime()
    cfg['end'] = en.tz_localize(None).to_pydatetime()


def build(top_n, sim_start, jobs, out_path, select='test'):
    from config import load_config

    cfg = load_config()
    _apply_segments(cfg)
    tf = cfg.get('tf', '1d')
    lib = os.path.join(_state_dir(), f'library{"" if tf == "1d" else "_" + tf}.jsonl')
    top = _pick_top(top_n, select, lib)
    if len(top) < 2:
        raise RuntimeError('need at least 2 scored alphas in the library')
    if tf == '1d':
        return _build_daily(cfg, top, sim_start, jobs, out_path, select)
    return _build_fast(cfg, top, out_path, select)


def _build_daily(cfg, top, sim_start, jobs, out_path, select):
    """1d: the real quantpylib Portfolio — the numbers paper/Serve will reproduce."""
    from evaluator import build_panel, basket_returns, open_pnl_series
    from quantpylib.simulator.alpha import Portfolio

    t0 = time.time()
    tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'))
    ts, te = cfg['splits']['test']
    # no --sim-start -> the FULL span (TRAIN start), so the doc carries all three segments
    start = (pd.Timestamp(sim_start, tz='UTC').tz_localize(None).to_pydatetime() if sim_start
             else cfg['start'])
    end = te.tz_localize(None).to_pydatetime()

    formulas = [c['formula'] for c in top]
    how = ('by TEST Sharpe (⚠ cherry-pick: combined TEST is optimistic)' if select != 'base'
           else 'by base=min(train,val) — TEST stays held out')
    print(f'· combining top-{len(formulas)} {how} (real engine, {jobs} workers)…', flush=True)

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
    segs = _seg_metrics(comb['capital_ret'], cfg['splits'])
    indiv = [(_metrics(sdf['capital_ret'], ts, te) or {}).get('sharpe') for sdf in stratdfs]
    bh = basket_returns(panel)
    bh_m = _metrics(bh, ts, te)
    bh_segs = _seg_metrics(bh, cfg['splits'])

    # equity over the WHOLE simulated span (combined + basket on the same grid)
    cr = comb['capital_ret'].fillna(0.0)
    ce = (1 + cr).cumprod()
    br = bh.reindex(ce.index).fillna(0.0); be = (1 + br).cumprod()
    dates = [d.strftime('%Y-%m-%d') for d in ce.index]

    # combined target weights over the WHOLE simulated span — the signals CSV labels every row
    # with its TRAIN/VAL/TEST segment, same as the single-alpha export
    present = [t for t in tk if f'{t} w' in comb.columns]
    cw_all = comb[[f'{t} w' for t in present]].rename(
        columns={f'{t} w': t for t in present})
    op = open_pnl_series(cw_all, panel['ret']).reindex(ce.index).fillna(0.0)
    cw = cw_all[cw_all.abs().sum(axis=1) > 0]             # drop empty days
    weights = {'dates': [d.strftime('%Y-%m-%d') for d in cw.index], 'tickers': present,
               'W': [[round(float(x), 5) for x in row] for row in cw.to_numpy()]}

    doc = {'ok': True, 'n': len(formulas), 'sel': select,   # 'test' | 'base'; docs without 'sel'
           'tf': '1d',
           'sim_start': str(pd.Timestamp(start).date()),    # predate the selectable picker (=test)
           'test': f'{ts.date()}..{te.date()}',
           'span': f'{ce.index[0].date()}..{te.date()}',
           'metrics': m, 'basket': bh_m, 'indiv_sharpe': indiv,
           'segments': segs, 'basket_segments': bh_segs, 'bounds': _bounds(cfg['splits']),
           'formulas': [f[:90] for f in formulas], 'formulas_full': formulas,
           'weights': weights, 'weights_span': 'full',   # the CSV export checks this stamp
           'open_pnl': [round(float(x), 5) for x in op.values],
           'equity': {'dates': dates, 'combined': [round(float(x), 5) for x in ce.values],
                      'basket': [round(float(x), 5) for x in be.values]},
           'built_secs': round(time.time() - t0, 1)}
    _write_doc(doc, out_path)
    print(f'✓ portfolio built: Sharpe {_seg_line(segs)} · {doc["built_secs"]}s → {out_path}',
          flush=True)
    return 0


def _build_fast(cfg, top, out_path, select):
    """Intraday: fastsim per member -> Σ(weight×leverage) -> the same kernel once more.
    The sum is exactly what Portfolio.compute_forecasts produces, so the second vol-targeting +
    inertia layer is applied with identical semantics — just in the search's engine, with the
    timeframe's annualization/EWMA. No multiprocessing: fastsim does a member in ~a second."""
    from genome import parse
    from evaluator import build_panel, basket_returns, make_market, eval_alpha_panel, open_pnl_series
    from fastsim import fast_sim_paths

    t0 = time.time()
    tf, ann, lam = cfg['tf'], cfg['ann'], cfg['ewma_lambda']
    formulas = [c['formula'] for c in top]
    how = ('by TEST Sharpe (⚠ cherry-pick: combined TEST is optimistic)' if select != 'base'
           else 'by base=min(train,val) — TEST stays held out')
    print(f'· combining top-{len(formulas)} {how} ({tf} fastsim — the search engine)…', flush=True)

    tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'),
                                 freq=cfg['freq'])
    market = make_market(panel, tk, raw, vol_window=cfg['vol_window'])
    ts, te = cfg['splits']['test']

    rets, paths = [], []
    for i, formula in enumerate(formulas):
        ap = eval_alpha_panel(parse(formula), panel)
        r, wl = fast_sim_paths(ap[tk].to_numpy(dtype=np.float64), market, cfg['vol'], cfg['exec'],
                               ann=ann, ewma_lambda=lam)
        rets.append(r)
        paths.append(wl)
        print(f'  [{i + 1}/{len(formulas)}] strategy S{i} simulated', flush=True)

    print('· running the Portfolio combiner (same kernel over Σ weight×leverage)…', flush=True)
    comb_alpha = np.sum(paths, axis=0)        # NaN pre-listing cells stay NaN -> masked eligibility
    comb_ret, comb_wl = fast_sim_paths(comb_alpha, market, cfg['vol'], cfg['exec'],
                                       ann=ann, ewma_lambda=lam)

    m = _metrics(comb_ret, ts, te, ann)
    segs = _seg_metrics(comb_ret, cfg['splits'], ann)
    indiv = [(_metrics(r, ts, te, ann) or {}).get('sharpe') for r in rets]
    bh = basket_returns(panel)
    bh_m = _metrics(bh, ts, te, ann)
    bh_segs = _seg_metrics(bh, cfg['splits'], ann)

    fmt = '%Y-%m-%d %H:%M'
    cr = comb_ret.fillna(0.0)                 # the WHOLE simulated span — all three segments
    ce = (1 + cr).cumprod()
    br = bh.reindex(ce.index).fillna(0.0)
    be = (1 + br).cumprod()
    step = max(1, len(ce) // 3000)            # chart payload cap (a 15m full span is ~200k bars)
    dates = [d.strftime(fmt) for d in ce.index[::step]]
    op = open_pnl_series(pd.DataFrame(comb_wl, index=market['index'], columns=list(tk)),
                         panel['ret']).reindex(ce.index).fillna(0.0)

    # per-bar dollar weights: w = wl / Σ|wl| (row gross); NaN cells (pre-listing) -> 0.
    # Intraday stays TEST-only ON PURPOSE: a full 15m span is ~100k bars and would blow the
    # status JSON to tens of MB (daily carries the full TRAIN/VAL/TEST span).
    idx = market['index']
    gross = np.nansum(np.abs(comb_wl), axis=1)
    W = np.nan_to_num(comb_wl / np.where(gross == 0, 1.0, gross)[:, None], nan=0.0)
    rows = (idx >= ts) & (idx < te) & (gross > 1e-12)
    weights = {'dates': [d.strftime(fmt) for d in idx[rows]], 'tickers': list(tk),
               'W': [[round(float(x), 5) for x in row] for row in W[rows]]}

    doc = {'ok': True, 'n': len(formulas), 'sel': select, 'tf': tf,
           'sim_start': str(pd.Timestamp(cfg['start']).date()),
           'test': f'{ts.date()}..{te.date()}',
           'span': f'{ce.index[0].date()}..{te.date()}',
           'metrics': m, 'basket': bh_m, 'indiv_sharpe': indiv,
           'segments': segs, 'basket_segments': bh_segs, 'bounds': _bounds(cfg['splits']),
           'formulas': [f[:90] for f in formulas], 'formulas_full': formulas,
           'weights': weights, 'weights_span': 'test',   # intraday: TEST-only (payload size)
           'open_pnl': [round(float(x), 5) for x in op.values[::step]],
           'equity': {'dates': dates, 'combined': [round(float(x), 5) for x in ce.values[::step]],
                      'basket': [round(float(x), 5) for x in be.values[::step]]},
           'built_secs': round(time.time() - t0, 1)}
    _write_doc(doc, out_path)
    print(f'✓ portfolio built ({tf}): Sharpe {_seg_line(segs)} · {doc["built_secs"]}s → {out_path}',
          flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description='Build a combined Portfolio from the top-N library alphas')
    ap.add_argument('--top', type=int, default=6)
    ap.add_argument('--select', choices=('test', 'base'), default='test',
                    help='ranking for the top-N: held-out TEST Sharpe (optimistic cherry-pick) '
                         'or fitness min(train,val) (TEST stays a clean evaluation)')
    ap.add_argument('--sim-start', default='',
                    help='simulation start; empty (default) = TRAIN start, so metrics/equity '
                         'cover all three segments. An explicit date (e.g. 2022-06-01) trades '
                         'the TRAIN/VAL view for build speed.')
    ap.add_argument('--jobs', type=int, default=0, help='parallel workers (0 = auto)')
    ap.add_argument('--out', default=os.path.join(_state_dir(), 'portfolio.json'))
    args = ap.parse_args()
    jobs = args.jobs if args.jobs > 0 else max(1, min(args.top, (os.cpu_count() or 4) - 2))
    try:
        rc = build(args.top, args.sim_start, jobs, args.out, args.select)
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
