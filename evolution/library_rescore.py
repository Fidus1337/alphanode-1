"""Library re-scoring: run ALL known formulas on a set of pairs — fast, without evolution.

Takes all champion formulas from the registry (experiments/registry.jsonl; fallback — champions.json),
runs each through fast_sim on the target universe and ranks by base = min(train,val) Sharpe.
TEST is printed FOR REFERENCE (it takes no part in selection) — each test peek increments a counter.
The result is written to champions.json (the current set) + to the registry as kind='rescore', so that
plot_champions.py / champion_entries.py / show_formula.py work downstream.

  python library_rescore.py                              # the library on the universe from config.ini
  python library_rescore.py --universe BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT
  python library_rescore.py --source ALL --top 15        # only formulas from runs on 'ALL'
"""
import os
import sys
import argparse
import warnings

import numpy as np

warnings.filterwarnings('ignore')
np.seterr(divide='ignore', invalid='ignore')

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from config import load_config                                  # noqa: E402
from evaluator import build_panel, make_market, evaluate        # noqa: E402
from genome import parse                                        # noqa: E402
import experiments                                              # noqa: E402


def _m(md):
    return {k: round(v, 4) for k, v in md.items()} if md else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--universe', help='target pairs, comma-separated (otherwise from config.ini)')
    ap.add_argument('--source', default=None,
                    help="filter the library by universe_key ('ALL' or 'BTC|ETH|...')")
    ap.add_argument('--top', type=int, default=15, help='how many of the best to save')
    args = ap.parse_args()

    cfg = load_config()
    if args.universe:
        cfg['instruments'] = [x.strip().upper() for x in args.universe.split(',') if x.strip()]

    experiments.bootstrap_from_champions()           # don't lose previous champions
    pool = experiments.strategy_pool(args.source)
    if not pool:
        print('Library is empty. First run run_evo.py (or check --source).')
        return

    tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'))
    market = make_market(panel, tk, raw)
    uni = experiments.universe_key(cfg.get('instruments'))
    print(f'Re-scoring {len(pool)} library formulas on universe: {uni} ({len(tk)} pairs)')
    print('-' * 70)

    results, seen = [], set()
    for i, f in enumerate(pool, 1):
        try:
            node = parse(f)
        except Exception:
            continue
        c = node.canon()
        if c in seen:
            continue
        seen.add(c)
        r = evaluate(node, tk, panel, market, cfg['splits'], cfg['vol'], cfg['exec'])
        if r is None:
            continue
        base = min(r['train_sharpe'], r['val_sharpe'])
        if not np.isfinite(base):
            continue
        results.append((base, r, f))
        if i % 25 == 0:
            print(f'  ...evaluated {i}/{len(pool)}')

    if not results:
        print('No formula produced a valid result on this universe.')
        return
    results.sort(key=lambda x: -x[0])                 # selection ONLY by base=min(train,val)
    top = results[:args.top]

    print(f'\nTop-{len(top)} of the library on {uni} (ranked by base; TEST — for reference):')
    print(f'{"#":>2} {"train":>6} {"val":>6} {"base":>6} {"|":>2} {"TEST":>7} {"tDD":>6}  formula')
    print('-' * 78)
    champions = []
    for i, (base, r, f) in enumerate(top):
        te = r['test']
        tes = f'{te["sharpe"]:+.2f}' if te else '  n/a'
        ted = f'{te["dd"] * 100:.0f}%' if te else '  n/a'
        print(f'{i:>2} {r["train_sharpe"]:>+6.2f} {r["val_sharpe"]:>+6.2f} {base:>+6.2f} {"|":>2} '
              f'{tes:>7} {ted:>6}  {f[:44]}')
        champions.append({'rank': i, 'formula': f, 'size': r['size'], 'base': round(base, 3),
                          'train': _m(r['train']), 'val': _m(r['val']), 'test': _m(r['test'])})
    print('-' * 78)

    rid, path = experiments.save_champions(cfg, champions, n_trials=len(seen), kind='rescore')
    peeks = experiments.bump_test_peeks(1)
    print(f'\nSaved to {os.path.basename(path)} + registry (id {rid}, universe {uni}).')
    print(f'Test hygiene: TEST has been peeked {peeks} time(s) already — keep in mind the multiple-'
          f'testing debt (read the best TEST with a discount; the final check is on the forward).')
    print('Next: plot_champions.py / champion_entries.py / show_formula.py work with this set.')


if __name__ == '__main__':
    main()
