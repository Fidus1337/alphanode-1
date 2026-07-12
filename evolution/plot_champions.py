"""Redraw evo_champions.png from champions.json WITHOUT re-running the search.

By default draws the top by TEST-Sharpe (who fired on the closed segment).
  python plot_champions.py                 # top-4 by TEST
  python plot_champions.py --by base       # top-4 by base (in-sample)
  python plot_champions.py --top 6         # more curves
"""
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from config import load_config                                             # noqa: E402
from evaluator import build_panel, make_market, simulate_returns, basket_returns  # noqa: E402
from genome import parse                                                   # noqa: E402
import report                                                              # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--by', choices=['test', 'base'], default='test', help='what to rank the curves by')
    ap.add_argument('--top', type=int, default=4, help='how many champions on the chart')
    args = ap.parse_args()

    cfg = load_config()
    champs = json.load(open(os.path.join(HERE, 'champions.json')))['champions']

    if args.by == 'test':
        pool = sorted([c for c in champs if c.get('test')], key=lambda c: -c['test']['sharpe'])
    else:
        pool = sorted(champs, key=lambda c: -c['base'])
    top = pool[:args.top]

    tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'))
    market = make_market(panel, tk, raw)
    basket = basket_returns(panel)

    returns = {}
    for c in top:
        r = simulate_returns(parse(c['formula']), tk, panel, market, cfg['vol'], cfg['exec'])
        if r is not None:
            te = c['test']['sharpe'] if c.get('test') else float('nan')
            returns[f'#{c["rank"]} TEST Sh {te:+.2f}  {c["formula"][:34]}'] = r

    tag = 'TEST' if args.by == 'test' else 'base'
    out = os.path.join(HERE, 'evo_champions.png')
    report.plot_equity(returns, basket, cfg['splits'], out,
                       f'Evolution champions (top-{len(returns)} by {tag}): equity TRAIN | VAL | TEST')
    print(f'Redrawn ({args.by}): {out}')
    for c in top:
        te = c['test']['sharpe'] if c.get('test') else float('nan')
        print(f'  #{c["rank"]:>2}  TEST {te:+.2f}  base {c["base"]:+.2f}  {c["formula"]}')


if __name__ == '__main__':
    main()
