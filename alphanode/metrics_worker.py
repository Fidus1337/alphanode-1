"""Leaderboard trade stats (long/short/win/activity on TEST), computed OUT OF PROCESS.

The GUI used to run this on a background thread, but the work is numpy/pandas-heavy PYTHON —
parsing a genome, evaluating it over the panel, running fast_sim — and it holds the GIL far more
than it releases it. The Tk main loop starves behind it: a status poll that costs 14ms turns into
~800ms and the window visibly stalls while the table fills in. A subprocess has its own GIL, so the
GUI only ever waits on a pipe (which releases it).

    python alphanode/metrics_worker.py      # config from ALPHANODE_* env + config.ini
    <exe> --role metrics                    # frozen build

stdin  -> {"formulas": [...], "instruments": [...]|null, "vol": .., "exec": ..,
           "train_start": "YYYY-MM-DD", "test_start": ..., "test_end": ...}
stdout -> {"ok": true, "metrics": {formula: {"long":n,"short":n,"win":f,"act":f} | "err"}}
           {"ok": false, "error": "..."}   on a failure that kills the whole batch

A formula that cannot be parsed or never trades comes back as "err" — same contract the GUI's
cache already speaks, so callers need no new branches.
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


def build_ctx(opt):
    """Panel/market + the TEST mask, exactly as the GUI's _metrics_ctx built them.
    Timeframe-aware: the grid freq / vol window / bars-per-year come from load_config
    (which honors ALPHANODE_TF), so intraday libraries get intraday-correct stats."""
    from config import load_config
    from evaluator import build_panel, make_market
    cfg = load_config()
    if opt.get('instruments'):
        cfg['instruments'] = list(opt['instruments'])
    vol = float(opt.get('vol', cfg['vol']))
    ex = float(opt.get('exec', cfg['exec']))
    ann = float(cfg.get('ann', 365.0))
    tr = pd.Timestamp(opt['train_start'], tz='UTC')
    te = pd.Timestamp(opt['test_start'], tz='UTC')
    en = pd.Timestamp(opt['test_end'], tz='UTC')
    tk, raw, panel = build_panel(cfg['data'], tr.tz_localize(None).to_pydatetime(),
                                 en.tz_localize(None).to_pydatetime(), cfg.get('instruments'),
                                 freq=cfg.get('freq', 'D'))
    market = make_market(panel, tk, raw, vol_window=cfg.get('vol_window', 30))
    tmask = (market['index'] >= te) & (market['index'] < en)
    elig = market['base_elig']
    n_assets = int(elig[tmask].any(axis=0).sum()) or int(elig.shape[1])   # assets live on TEST
    years = max(float(np.count_nonzero(tmask)) / ann, 1e-9)
    return {'panel': panel, 'market': market, 'V': market['V'], 'elig': elig, 'tmask': tmask,
            'n_assets': max(1, n_assets), 'years': years, 'vol': vol, 'exec': ex,
            'ann': ann, 'ewma': float(cfg.get('ewma_lambda', 0.06))}


def trade_stats(formula, ctx):
    """{long, short, win, act} for one formula on TEST — act = trades per asset per year
    (relative activity, universe/period independent). 'err' if it doesn't parse or never trades."""
    from genome import parse
    from evaluator import eval_alpha_panel
    from fastsim import fast_sim
    market, V, elig, tmask = ctx['market'], ctx['V'], ctx['elig'], ctx['tmask']
    try:
        raw = eval_alpha_panel(parse(formula), ctx['panel'])[market['tk']].to_numpy(dtype=np.float64)
        A = pd.DataFrame(raw).ffill().to_numpy()
        E = elig & np.isfinite(A)
        fc = np.where(E, np.where(E, A, 0.0) / V, 0.0)
        chips = np.nansum(np.abs(fc), axis=1, keepdims=True)
        W = fc / np.where(chips == 0.0, 1.0, chips)                      # + long / − short
        side = np.where(W > 0.0005, 1, np.where(W < -0.0005, -1, 0))     # daily side [T,N]
        if not np.abs(side[tmask]).any():                               # no positions on TEST — invalid
            return 'err'
        # a "trade" = opening a position: cross into long/short from flat/opposite
        prev = np.vstack([np.zeros((1, side.shape[1])), side[:-1]])      # previous calendar day
        long_tr = int(((side == 1) & (prev != 1))[tmask].sum())         # long entries in TEST
        short_tr = int(((side == -1) & (prev != -1))[tmask].sum())      # short entries in TEST
        rt = fast_sim(raw, market, ctx['vol'], ctx['exec'],
                      ann=ctx['ann'], ewma_lambda=ctx['ewma']).to_numpy()[tmask]
        active = np.abs(rt) > 1e-9                                       # days when something happened
        win = float((rt[active] > 0).mean()) if active.any() else 0.0
        act = (long_tr + short_tr) / ctx['n_assets'] / ctx['years']     # trades / asset / year
        return {'long': long_tr, 'short': short_tr, 'win': win, 'act': act}
    except Exception:                                                   # noqa: BLE001
        return 'err'


def main():
    try:
        opt = json.load(sys.stdin)
    except Exception as e:                               # noqa: BLE001
        print(json.dumps({'ok': False, 'error': f'bad input: {e}'}))
        return
    formulas = [f for f in (opt.get('formulas') or []) if f]
    if not formulas:
        print(json.dumps({'ok': True, 'metrics': {}}))
        return
    try:
        ctx = build_ctx(opt)
    except Exception as e:                               # noqa: BLE001 — no data/config: the GUI
        print(json.dumps({'ok': False,                   # marks the whole batch 'err' and moves on
                          'error': f'{type(e).__name__}: {e}'}))
        return
    out = {f: trade_stats(f, ctx) for f in formulas}
    print(json.dumps({'ok': True, 'metrics': out}))


if __name__ == '__main__':
    main()
