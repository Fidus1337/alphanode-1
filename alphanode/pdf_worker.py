"""Render the analytics PDF dashboard OUT OF PROCESS.

The GUI first tried to build the report on a background thread. It segfaults: a 4-page
report renders a lot of text (tables, dozens of labels, annotations), and matplotlib's
FreeType text rasterization is not safe to run concurrently with the Tk main loop, which
also drives Xft/FreeType — the X error is asynchronous and lands as a SIGSEGV somewhere
unrelated. Same class of bug the leaderboard metrics hit; same fix: a subprocess has its
own address space (no shared FreeType with Tk) and its own GIL, so the GUI only waits on a
pipe.

    python alphanode/pdf_worker.py      # config from ALPHANODE_* env + config.ini
    <exe> --role pdfreport              # frozen build

stdin  -> {"kind": "alpha"|"portfolio", "out": "/path.pdf", "title": .., "subtitle": ..,
           "stamp": .., "vol": .., "exec": .., "instruments": [...]|null,
           "train_start": "YYYY-MM-DD", "val_start": .., "test_start": .., "test_end": ..,
           # kind=alpha:      "formula": "...", "seg_metrics": {"train":{..},"val":..,"test":..}
           # kind=portfolio:  "doc": <portfolio.json>}
stdout -> {"ok": true, "info": {"pages": 4, "days": N, "path": "..."}}
          {"ok": false, "error": "..."}
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
for _p in (os.path.join(PROJ, 'evolution'), PROJ, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import warnings                                          # noqa: E402
import numpy as np                                       # noqa: E402
import pandas as pd                                      # noqa: E402
warnings.filterwarnings('ignore'); np.seterr(all='ignore')

import matplotlib                                        # noqa: E402
matplotlib.use('Agg')


def _splits(opt):
    try:
        tr = pd.Timestamp(opt['train_start'], tz='UTC')
        va = pd.Timestamp(opt['val_start'], tz='UTC')
        te = pd.Timestamp(opt['test_start'], tz='UTC')
        en = pd.Timestamp(opt['test_end'], tz='UTC')
        return {'train': (tr, va), 'val': (va, te), 'test': (te, en)}, (tr, en)
    except (KeyError, ValueError):
        return None, None


def _alpha_weights(formula, market, panel):
    """Inverse-vol + chips normalized target weights — the same the engine trades and the
    GUI's _alpha_weights_wide / metrics_worker build."""
    from genome import parse
    from evaluator import eval_alpha_panel
    ap = eval_alpha_panel(parse(formula), panel)
    A = pd.DataFrame(ap[market['tk']].to_numpy(dtype=np.float64)).ffill().to_numpy()
    V = market['V']
    E = market['base_elig'] & np.isfinite(A)
    fc = np.where(E, np.where(E, A, 0.0) / V, 0.0)
    chips = np.nansum(np.abs(fc), axis=1, keepdims=True)
    W = fc / np.where(chips == 0.0, 1.0, chips)
    return pd.DataFrame(np.round(W, 6), index=market['index'], columns=market['tk'])


def _build_alpha(opt, pdf_report):
    from config import load_config
    from evaluator import build_panel, make_market, basket_returns, simulate_returns
    from genome import parse
    cfg = load_config()                                  # timeframe fields honor ALPHANODE_TF
    instruments = opt.get('instruments') or cfg.get('instruments')
    vol = float(opt.get('vol', cfg['vol']))
    ex = float(opt.get('exec', cfg['exec']))
    ann = float(cfg.get('ann', 365.0))
    splits, span = _splits(opt)
    start, end = (span[0].tz_localize(None).to_pydatetime(),
                  span[1].tz_localize(None).to_pydatetime()) if span else (cfg['start'], cfg['end'])
    tk, raw, panel = build_panel(cfg['data'], start, end, instruments, freq=cfg.get('freq', 'D'))
    market = make_market(panel, tk, raw, vol_window=cfg.get('vol_window', 30))
    wide = _alpha_weights(opt['formula'], market, panel)
    r = simulate_returns(parse(opt['formula']), tk, panel, market, vol, ex,
                         ann=ann, ewma_lambda=float(cfg.get('ewma_lambda', 0.06)))
    basket = basket_returns(panel)
    return pdf_report.build_report(
        opt['out'], title=opt.get('title', 'AlphaNode'), subtitle=opt.get('subtitle', ''),
        wide=wide, rets=r, basket=basket, splits=splits,
        seg_metrics=opt.get('seg_metrics') or {}, asset_rets=panel['ret'][tk],
        exec_cost=ex, stamp=opt.get('stamp', ''), ann=ann)


def _build_portfolio(opt, pdf_report):
    doc = opt['doc']
    w = doc['weights']
    idx = pd.to_datetime(w['dates'])
    wide = pd.DataFrame(np.array(w['W'], dtype=float), index=idx, columns=w['tickers'])
    rets = basket_r = None
    eq = doc.get('equity') or {}
    if eq.get('dates'):
        eidx = pd.to_datetime(eq['dates'])
        ce = pd.Series(eq['combined'], index=eidx, dtype=float)
        rets = ce.pct_change()
        rets.iloc[0] = ce.iloc[0] - 1.0                  # ce = cumprod(1+r) from day one
        if eq.get('basket'):
            be = pd.Series(eq['basket'], index=eidx, dtype=float)
            basket_r = be.pct_change()
            basket_r.iloc[0] = be.iloc[0] - 1.0
    splits = None
    try:                                                 # doc['test'] = 'YYYY-MM-DD..YYYY-MM-DD'
        a, b = str(doc.get('test', '')).split('..')
        splits = {'test': (pd.Timestamp(a), pd.Timestamp(b))}
    except ValueError:
        pass
    asset_rets = None
    try:                                                 # per-asset returns for attribution (optional)
        from config import load_config
        from evaluator import build_panel
        cfg = load_config()
        start = idx[0].to_pydatetime()
        end = (idx[-1] + pd.Timedelta(days=1)).to_pydatetime()
        _tk, _raw, panel = build_panel(cfg['data'], start, end, w['tickers'])
        cols = [t for t in w['tickers'] if t in panel['ret'].columns]
        asset_rets = panel['ret'][cols]
    except Exception:                                    # noqa: BLE001
        pass
    try:                                                 # bars/year for the current timeframe
        from config import load_config
        ann = float(load_config().get('ann', 365.0))
    except Exception:                                    # noqa: BLE001
        ann = None
    return pdf_report.build_report(
        opt['out'], title=opt.get('title', 'AlphaNode'), subtitle=opt.get('subtitle', ''),
        wide=wide, rets=rets, basket=basket_r, splits=splits,
        seg_metrics={'test': dict(doc.get('metrics') or {})}, asset_rets=asset_rets,
        exec_cost=float(opt.get('exec', 0.001)), stamp=opt.get('stamp', ''), ann=ann)


def main():
    try:
        opt = json.load(sys.stdin)
    except Exception as e:                               # noqa: BLE001
        print(json.dumps({'ok': False, 'error': f'bad input: {e}'}))
        return
    try:
        import pdf_report
        if opt.get('kind') == 'portfolio':
            info = _build_portfolio(opt, pdf_report)
        else:
            info = _build_alpha(opt, pdf_report)
        print(json.dumps({'ok': True, 'info': info}))
    except Exception as e:                               # noqa: BLE001
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({'ok': False, 'error': f'{type(e).__name__}: {e}'}))


if __name__ == '__main__':
    main()
