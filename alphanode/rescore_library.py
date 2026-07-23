"""Re-score the whole alphanode library with the CURRENT metric convention.

Needed after the 2026-07 metrics fix: _metrics used to drop zero-return days before
annualizing, inflating Sharpe by ~1/sqrt(active_fraction) and CAGR far more — every stored
train/val/test/base in library.jsonl carries that bias, so old rows are not comparable with
newly mined ones. This tool recomputes every formula through the same evaluate() path the
search uses (honest calendar metrics), rewrites library.jsonl atomically, and drops rows that
are degenerate under the honest rules (never really traded). A one-time backup of the original
file is kept as library.jsonl.bak (never overwritten by later runs).

    python alphanode/rescore_library.py            # state dir from ALPHANODE_STATE_DIR (or alphanode/state)
    <exe> --role rescore                           # frozen build
"""
import os
import sys
import json
import time
import multiprocessing as mp

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
for _p in (os.path.join(PROJ, 'evolution'), PROJ, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import warnings                                          # noqa: E402
import numpy as np                                       # noqa: E402
warnings.filterwarnings('ignore'); np.seterr(all='ignore')


def _state_dir():
    return os.environ.get('ALPHANODE_STATE_DIR') or os.path.join(HERE, 'state')


_G = {}


def _winit():
    from config import load_config
    from evaluator import build_panel, make_market
    cfg = load_config()                                  # timeframe fields honor ALPHANODE_TF
    uni = os.environ.get('ALPHANODE_UNIVERSE', 'all')
    if uni.lower() not in ('all', '*', ''):
        cfg['instruments'] = [x.strip().upper() for x in uni.split(',') if x.strip()]
    tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'),
                                 freq=cfg.get('freq', 'D'))
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    _G.update(cfg=cfg, tk=tk, panel=panel,
              market=make_market(panel, tk, raw, vol_window=cfg.get('vol_window', 30)))


def _rescore_one(row):
    from genome import parse
    from evaluator import evaluate
    cfg = _G['cfg']
    try:
        res = evaluate(parse(row['formula']), _G['tk'], _G['panel'], _G['market'],
                       cfg['splits'], cfg['vol'], cfg['exec'],
                       ann=cfg.get('ann', 365.0), ewma_lambda=cfg.get('ewma_lambda', 0.06))
    except Exception:                                    # noqa: BLE001
        res = None
    if res is None:                                      # degenerate under honest rules
        return None
    rm = lambda m: ({k: round(float(v), 4) for k, v in m.items()} if m else None)  # noqa: E731
    out = dict(row)
    out['train'], out['val'], out['test'] = rm(res['train']), rm(res['val']), rm(res['test'])
    out['base'] = round(min(res['train_sharpe'], res['val_sharpe']), 3)
    return out


def main():
    tf = (os.environ.get('ALPHANODE_TF') or '1d').strip().lower()
    suffix = '' if tf == '1d' else f'_{tf}'
    lib = os.path.join(_state_dir(), f'library{suffix}.jsonl')
    if not os.path.exists(lib):
        print(f'no library at {lib} — nothing to rescore')
        return
    rows = []
    for line in open(lib, encoding='utf-8'):
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if not rows:
        print('library is empty — nothing to rescore')
        return

    bak = lib + '.bak'
    if not os.path.exists(bak):                          # one-time backup of the ORIGINAL scores
        with open(bak, 'w', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        print(f'backup: {bak}')

    jobs = max(1, (os.cpu_count() or 4) // 2)
    print(f'rescoring {len(rows)} alphas with honest calendar metrics ({jobs} workers)…', flush=True)
    t0 = time.time()
    with mp.Pool(processes=jobs, initializer=_winit) as pool:
        scored = []
        for i, out in enumerate(pool.imap(_rescore_one, rows, chunksize=8), 1):
            scored.append(out)
            if i % 100 == 0 or i == len(rows):
                print(f'  {i}/{len(rows)}', flush=True)
    kept = [r for r in scored if r is not None]
    dropped = len(scored) - len(kept)

    tmp = lib + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmp, lib)

    kept.sort(key=lambda c: c.get('base') if c.get('base') is not None else -1e9, reverse=True)
    print(f'✓ rescored {len(kept)} kept, {dropped} degenerate dropped · {time.time()-t0:.0f}s')
    print('new top-5 by base:')
    for c in kept[:5]:
        te = (c.get('test') or {}).get('sharpe')
        print(f"  base {c.get('base'):+.2f} · TEST {te if te is not None else '—'} · {c['formula'][:70]}")


if __name__ == '__main__':
    main()
