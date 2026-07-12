"""Show ANY formula as a strategy: per-segment metrics + equity chart (+ signal).

  python show_formula.py "ts_roc:14(ema:20(volume))"
  python show_formula.py "<formula>" --signal BTCUSDT ETHUSDT   # plus a chart of the raw alpha

Take a formula from champions.json or write your own from primitives (see primitives.py).
Settings (vol/fees/segments/universe) come from config.ini, the same as in the search.
"""
import os
import sys
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from config import load_config                                                     # noqa: E402
from evaluator import (build_panel, make_market, simulate_returns,                 # noqa: E402
                       basket_returns, eval_alpha_panel, _metrics)
from genome import parse                                                           # noqa: E402
import report                                                                      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('formula', help='the signal formula in quotes')
    ap.add_argument('--signal', nargs='*', default=[], metavar='TICKER',
                    help='also plot the raw signal for these tickers')
    args = ap.parse_args()

    cfg = load_config()
    tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'))
    market = make_market(panel, tk, raw)

    node = parse(args.formula)                      # string -> tree (syntax check)
    ret = simulate_returns(node, tk, panel, market, cfg['vol'], cfg['exec'])
    if ret is None:
        print('Formula is invalid or degenerate (does not trade / all NaN).')
        return

    sp = cfg['splits']
    print(f'\nFormula: {args.formula}')
    print(f'Size: {node.size()} nodes | universe: {len(tk)} pairs | vol {cfg["vol"]} fee {cfg["exec"]}')
    print('-' * 56)
    print(f'{"segment":8s}{"Sharpe":>9s}{"DD":>8s}{"CAGR":>9s}')
    for name, (lo, hi) in sp.items():
        m = _metrics(ret[(ret.index >= lo) & (ret.index < hi)])
        if m:
            print(f'{name:8s}{m["sharpe"]:>+9.2f}{m["dd"]*100:>7.0f}%{m["cagr"]*100:>8.1f}%')
        else:
            print(f'{name:8s}{"n/a":>9s}')
    print('-' * 56)

    basket = basket_returns(panel)
    eq_path = os.path.join(HERE, 'formula_equity.png')
    report.plot_equity({f'formula  {args.formula[:38]}': ret}, basket, sp, eq_path,
                       'Single formula: growth of $1 (NET) with TRAIN | VAL | TEST zones')
    print(f'Equity chart: {eq_path}')

    if args.signal:
        alpha = eval_alpha_panel(node, panel)       # wide table of the raw signal
        sig = {t: alpha[t] for t in args.signal if t in alpha.columns}
        if sig:
            sig_path = os.path.join(HERE, 'formula_signal.png')
            report.plot_signal(sig, sp, sig_path, f'Raw signal (alpha): {", ".join(sig)}')
            print(f'Signal chart: {sig_path}')
        else:
            print(f'No such tickers in the universe: {args.signal}')


if __name__ == '__main__':
    main()
