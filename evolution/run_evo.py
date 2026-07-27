"""Evolutionary search for trading strategies on top of the quantpylib engine.

Each genome is an alpha-signal formula; the engine turns it into positions with vol-targeting
and fees. Evolution selects ROBUST formulas (fitness = min(train,val) Sharpe),
penalizes complexity and clones; TEST is never used for selection and is only reported at the end.

All settings live in config.ini (see the config.py module). CLI flags override the file.

Run:
  python run_evo.py                 # full run per config.ini
  python run_evo.py --smoke         # quick smoke test (small population, 1 core)
  python run_evo.py --pop 120 --gens 15 --jobs 8 --seed 3
  python run_evo.py --config my.ini # a different config file
"""
import os
import sys
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from config import load_config                                 # noqa: E402
from evolution import evolve                                   # noqa: E402
from evaluator import build_panel, make_market, simulate_returns, basket_returns, _metrics  # noqa: E402
import report                                                  # noqa: E402
import experiments                                             # noqa: E402
from genome import parse as _parse                             # noqa: E402


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--config', help='path to the ini config (default: evolution/config.ini)')
    p.add_argument('--smoke', action='store_true', help='quick smoke test')
    p.add_argument('--pop', type=int)
    p.add_argument('--gens', type=int)
    p.add_argument('--jobs', type=int)
    p.add_argument('--seed', type=int)
    p.add_argument('--seed-from-library', action='store_true',
                   help='warm-start: seed the population with champions from the registry')
    p.add_argument('--seed-source', default=None,
                   help="where to take the seed from: universe_key ('ALL' or 'BTCUSDT|...'); default: all")
    p.add_argument('--advisor', action='store_true',
                   help='enable the neuro-symbolic advisor (LLM proposes formulas on plateaus; '
                        'needs ANTHROPIC_API_KEY)')
    return p.parse_args()


def fmt(m, key):
    return f'{m[key]:+.2f}' if m else '  n/a'


def main():
    args = parse_args()
    cfg = load_config(args.config)
    if args.smoke:
        cfg.update(pop=24, gens=4, n_jobs=1, hof_cap=8, random_inject=4, elitism=3)
    for k, a in (('pop', args.pop), ('gens', args.gens), ('n_jobs', args.jobs), ('seed', args.seed)):
        if a is not None:
            cfg[k] = a
    if args.advisor:
        cfg['advisor'] = True

    experiments.bootstrap_from_champions()           # save previous champions into the registry
    kind = 'evolve'
    if args.seed_from_library:                       # warm-start: seeding from the library
        pool = experiments.strategy_pool(args.seed_source)
        seeds = []
        for f in pool:
            try:
                seeds.append(_parse(f))
            except Exception:
                pass
        cfg['seed_formulas'] = seeds
        kind = 'evolve-warm'
        print(f'Warm-start: seeding from the library — {len(seeds)} formulas '
              f'(source: {args.seed_source or "all"}).')

    print('=' * 74)
    print('EVOLUTIONARY STRATEGY SEARCH')
    print('=' * 74)
    print(f'  Config: {os.path.abspath(args.config) if args.config else os.path.join(HERE, "config.ini")}'
          + ('  (+smoke)' if args.smoke else ''))
    uni = cfg.get('instruments')
    print(f'  Universe: {("all from data.pickle" if not uni else f"{len(uni)} pairs: " + ", ".join(uni))}')
    if cfg.get('advisor'):
        print(f'  Advisor: ON — {cfg["advisor_model"]} (patience {cfg["advisor_patience"]}, '
              f'max {cfg["advisor_max_calls"]} calls)')
    sp = cfg['splits']
    print(f'  TRAIN {sp["train"][0].date()}..{sp["train"][1].date()}  '
          f'VAL {sp["val"][0].date()}..{sp["val"][1].date()}  '
          f'TEST {sp["test"][0].date()}..{sp["test"][1].date()} (closed)')
    print(f'  pop={cfg["pop"]} gens={cfg["gens"]} depth<={cfg["max_depth"]} '
          f'size<={cfg["max_size"]} jobs={cfg["n_jobs"]} seed={cfg["seed"]}')
    print(f'  fitness = min(train,val) Sharpe - {cfg["parsimony"]}*size - correlation_penalty')
    print('-' * 74)

    hof, history, cache = evolve(cfg, log=print)
    n_trials = len(cache)

    if not hof:
        print('\nNo champions found (all genomes degenerate). Increase pop/gens.')
        return

    # ---- final report: TEST is REVEALED here (it was computed alongside but never consumed
    # by fitness/selection/seeding — see evaluator.evaluate / hof_update) ----
    print('\n' + '=' * 74)
    print(f'HALL OF FAME — {len(hof)} champions (out of {n_trials} unique formulas)')
    print('=' * 74)
    hdr = f'{"#":>2} {"train":>6} {"val":>6} {"base":>6} {"|":>2} {"TEST":>6} {"tDD":>6} {"tCAGR":>7}  formula'
    print(hdr)
    print('-' * 74)
    champions = []
    for i, h in enumerate(hof):
        te = h['test']
        row = (f'{i:>2} {h["train"]["sharpe"]:>+6.2f} {h["val"]["sharpe"]:>+6.2f} '
               f'{h["base"]:>+6.2f} {"|":>2} {fmt(te, "sharpe"):>6} '
               f'{(te["dd"]*100 if te else float("nan")):>5.0f}% '
               f'{(te["cagr"]*100 if te else float("nan")):>6.1f}%  {h["canon"]}')
        print(row)
        champions.append({
            'rank': i, 'formula': h['canon'], 'size': h['size'], 'base': round(h['base'], 3),
            'train': {k: round(v, 4) for k, v in h['train'].items()},
            'val': {k: round(v, 4) for k, v in h['val'].items()},
            'test': ({k: round(v, 4) for k, v in te.items()} if te else None),
        })
    print('-' * 74)

    # ---- basket for context ----
    tk, _raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'))
    market = make_market(panel, tk, _raw)
    basket = basket_returns(panel)
    b_test = basket[(basket.index >= sp['test'][0]) & (basket.index < sp['test'][1])]
    bm = _metrics(b_test)
    print(f'Basket (EW) on TEST: Sharpe {fmt(bm, "sharpe")}  '
          f'DD {bm["dd"]*100:.0f}%  CAGR {bm["cagr"]*100:.1f}%' if bm else 'Basket: n/a')

    # ---- honest verdict + multiple-testing correction ----
    best = hof[0]
    te = best['test']
    print('\nVERDICT:')
    print(f'  Best by base: train {best["train"]["sharpe"]:+.2f} / val {best["val"]["sharpe"]:+.2f} '
          f'-> TEST {fmt(te, "sharpe")}  (haircut on the closed segment)')
    print(f'  NOTE: {n_trials} formulas searched -> strong multiple testing.')
    print(f'  The best TEST-Sharpe of the selected set should be read with a discount; the real check')
    print(f'  is a forward/paper run on NEW data. TEST here is an estimate, not a guarantee.')

    # ---- save champions (champions.json) + append to the permanent registry ----
    rid, path = experiments.save_champions(cfg, champions, n_trials, kind=kind)
    print(f'\nChampions: {path} | registry entry: {rid} '
          f'(universe {experiments.universe_key(cfg.get("instruments"))})')

    # ---- charts ----
    report.plot_history(history, os.path.join(HERE, 'evo_progress.png'))
    # for the chart we take the top-4 by TEST-Sharpe (who actually fired on the closed segment);
    # the #i number is the rank by base (as in champions.json); selection is still by base.
    ranked = [(i, h) for i, h in enumerate(hof) if h['test']]
    topk = sorted(ranked, key=lambda ih: -ih[1]['test']['sharpe'])[:4]
    returns = {}
    for i, h in topk:
        r = simulate_returns(_parse(h['canon']), tk, panel, market, cfg['vol'], cfg['exec'])
        if r is not None:
            returns[f'#{i} TEST Sh {h["test"]["sharpe"]:+.2f}  {h["canon"][:34]}'] = r
    report.plot_equity(returns, basket, sp, os.path.join(HERE, 'evo_champions.png'),
                       'Evolution champions (top-4 by TEST): equity with TRAIN | VAL | TEST zones')
    print(f'Charts: {os.path.join(HERE, "evo_progress.png")} , '
          f'{os.path.join(HERE, "evo_champions.png")}')


if __name__ == '__main__':
    main()
