"""Check: fast_sim must match the real run_simulation engine.

We run several genomes through both paths and compare the NET-return series: correlation,
max|difference| and Sharpe by segment. If they match, fast_sim can be trusted as the fitness,
while the engine is kept for the final validation of champions.
"""
import time
from datetime import datetime

import numpy as np

from evaluator import build_panel, eval_alpha_panel, PrecomputedAlpha
from fastsim import precompute_market, fast_sim
from genome import Node, random_tree
import random

VOL, EXEC = 0.30, 0.001


def engine_returns(node, tk, raw, panel):
    ap = eval_alpha_panel(node, panel)
    dfs = {t: raw[t].copy() for t in tk}
    s, e = panel['close'].index[0], panel['close'].index[-1]
    a = PrecomputedAlpha(alpha_panel=ap, insts=tk, dfs=dfs,
                         start=s.to_pydatetime().replace(tzinfo=None),
                         end=e.to_pydatetime().replace(tzinfo=None),
                         portfolio_vol=VOL, execrates=EXEC)
    port = a.run_simulation()
    r = port['capital'].pct_change().fillna(0.0)
    r.index = port.index
    return r


def fast_returns(node, tk, panel, market):
    ap = eval_alpha_panel(node, panel)
    return fast_sim(ap[tk].to_numpy(dtype=np.float64), market, VOL, EXEC)


def sharpe(r):
    r = r[r != 0]
    return (r.mean() * 365) / (r.std() * np.sqrt(365)) if len(r) > 5 and r.std() else float('nan')


def main():
    tk, raw, panel = build_panel('../data.pickle', datetime(2019, 9, 5), datetime(2026, 6, 30))
    market = precompute_market(panel, tk, raw)
    # the REAL engine doesn't account funding PnL (phase 2) — parity is checked on price PnL,
    # so zero out F here; funding-aware vs funding-less kernels are compared in verify_numba.
    market['F'] = np.zeros_like(market['C'])

    genomes = [
        ('Bollinger', Node('ts_zscore', [Node('close')], 14)),
        ('MA-cross', Node('sign', [Node('sub', [Node('ts_mean', [Node('close')], 10),
                                                Node('ts_mean', [Node('close')], 20)])])),
        ('cs_mom', Node('cs_rank', [Node('ts_roc', [Node('close')], 30)])),
    ]
    rng = random.Random(11)
    for i in range(3):
        genomes.append((f'rand{i}', random_tree(rng, 5, 0.25)))

    print(f'{"genome":12s}{"corr":>8s}{"maxdiff":>11s}{"eng_Sh":>9s}{"fast_Sh":>9s}{"t_eng":>8s}{"t_fast":>8s}')
    print('-' * 66)
    for name, g in genomes:
        t0 = time.time(); re = engine_returns(g, tk, raw, panel); teng = time.time() - t0
        t0 = time.time(); rf = fast_returns(g, tk, panel, market); tfast = time.time() - t0
        al = re.align(rf, join='inner')
        a, b = al[0].to_numpy(), al[1].to_numpy()
        m = np.isfinite(a) & np.isfinite(b)
        c = np.corrcoef(a[m], b[m])[0, 1] if m.sum() > 5 else float('nan')
        md = np.max(np.abs(a[m] - b[m]))
        print(f'{name:12s}{c:>8.4f}{md:>11.2e}{sharpe(re):>9.3f}{sharpe(rf):>9.3f}{teng:>8.2f}{tfast:>8.3f}')


if __name__ == '__main__':
    main()
