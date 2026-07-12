"""Final validation of champions on the REAL run_simulation engine (not on fast-sim).

Reads champions.json, runs the top-K formulas through make_evolved()+engine and prints
train/val/TEST Sharpe. If it matches the fast-sim from champions.json, the proxy is honest,
and the evolved_strategy bridge is ready for eval_strategies.py / paper_trade.py.

Run: python validate_champions.py [K]
"""
import os
import sys
import json
import warnings

import numpy as np

warnings.filterwarnings('ignore')
np.seterr(divide='ignore', invalid='ignore')

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from config import load_config                     # noqa: E402
from evaluator import build_panel, _metrics       # noqa: E402
from evolved_strategy import make_evolved          # noqa: E402

_cfg = load_config()                               # same vol/fees/segments as in the search
VOL, EXEC = _cfg['vol'], _cfg['exec']
SPLITS = _cfg['splits']


def seg(ret, lo, hi):
    return ret[(ret.index >= lo) & (ret.index < hi)]


def main():
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    champs = json.load(open(os.path.join(HERE, 'champions.json')))['champions'][:k]

    tk, raw, panel = build_panel(_cfg['data'], _cfg['start'], _cfg['end'], _cfg.get('instruments'))
    start, end = panel['close'].index[0], panel['close'].index[-1]

    print(f'{"#":>2} {"size":>6} | {"train":>13} | {"val":>13} | {"TEST (engine vs proxy)":>22}')
    print('-' * 78)
    for c in champs:
        Cls = make_evolved(c['formula'], f'Champ{c["rank"]}')
        a = Cls(insts=tk, dfs={t: raw[t].copy() for t in tk},
                start=start.to_pydatetime().replace(tzinfo=None),
                end=end.to_pydatetime().replace(tzinfo=None),
                portfolio_vol=VOL, execrates=EXEC)
        port = a.run_simulation()
        ret = port['capital'].pct_change().fillna(0.0)

        mt = _metrics(seg(ret, *SPLITS['train']))
        mv = _metrics(seg(ret, *SPLITS['val']))
        me = _metrics(seg(ret, *SPLITS['test']))
        proxy = c['test']['sharpe'] if c['test'] else float('nan')
        print(f'{c["rank"]:>2} {c["size"]:>6} | '
              f'engine {mt["sharpe"]:>+5.2f} (px {c["train"]["sharpe"]:>+5.2f}) | '
              f'engine {mv["sharpe"]:>+5.2f} (px {c["val"]["sharpe"]:>+5.2f}) | '
              f'engine {me["sharpe"]:>+5.2f}  proxy {proxy:>+5.2f}')
    print('-' * 78)
    print('If engine≈proxy — fast-sim is honest and the evolved_strategy bridge works.')


if __name__ == '__main__':
    main()
