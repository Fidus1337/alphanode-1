"""Check: the numba-compiled fast_sim kernel must match the pure-numpy kernel (and be much faster).

The numba path only speeds up the fitness; it must not change a single number. We run many genomes
through both kernels and compare the NET-return series (correlation, max|difference|), then report
the speedup. Run:  python verify_numba.py
"""
import time
import random

import numpy as np
import pandas as pd

from config import load_config
from evaluator import build_panel, eval_alpha_panel
from genome import Node, random_tree
import fastsim
from fastsim import precompute_market, _sim_kernel_impl

VOL, EXEC, INERTIA, ANN, LAMBDA = 0.30, 0.001, 0.10, 365, 0.06


def _prep(node, tk, panel, market):
    """Reproduce fast_sim's input prep, returning (A, E) for the kernels."""
    ap = eval_alpha_panel(node, panel)
    arr = ap[tk].to_numpy(dtype=np.float64)
    A = pd.DataFrame(arr).ffill().to_numpy(dtype=np.float64)
    E = market['base_elig'] & np.isfinite(A)
    return A, E


def _returns(capital, index):
    return pd.Series(capital, index=index).pct_change().fillna(0.0).to_numpy()


def main():
    cfg = load_config()
    tk, raw, panel = build_panel(cfg['data'], cfg['start'], cfg['end'], cfg.get('instruments'))
    market = precompute_market(panel, tk, raw)
    C, R, V = market['C'], market['R'], market['V']
    idx = market['index']

    if fastsim._kernel_jit is None:
        print('numba NOT available — nothing to compare (fast_sim is running the numpy fallback).')
        return

    genomes = [
        ('Bollinger', Node('ts_zscore', [Node('close')], 14)),
        ('MA-cross', Node('sign', [Node('sub', [Node('ts_mean', [Node('close')], 10),
                                                Node('ts_mean', [Node('close')], 20)])])),
        ('cs_mom', Node('cs_rank', [Node('ts_roc', [Node('close')], 30)])),
    ]
    rng = random.Random(11)
    while len(genomes) < 40:
        g = random_tree(rng, cfg['max_depth'], term_prob=0.25)
        if 3 <= g.size() <= cfg['max_size']:
            genomes.append((f'rand{len(genomes)}', g))

    jit = fastsim._kernel_jit
    # prep everything first, and warm up the JIT (first call compiles) so timing is fair
    prepped = [(name, *_prep(g, tk, panel, market)) for name, g in genomes]
    jit(prepped[0][1], C, R, V, prepped[0][2], VOL, EXEC, INERTIA, ANN, LAMBDA)

    worst_corr, worst_diff = 1.0, 0.0
    t_py = t_jit = 0.0
    for name, A, E in prepped:
        t0 = time.perf_counter(); cap_py = _sim_kernel_impl(A, C, R, V, E, VOL, EXEC, INERTIA, ANN, LAMBDA)
        t1 = time.perf_counter(); cap_jit = jit(A, C, R, V, E, VOL, EXEC, INERTIA, ANN, LAMBDA)
        t2 = time.perf_counter()
        t_py += t1 - t0; t_jit += t2 - t1
        rp, rj = _returns(cap_py, idx), _returns(cap_jit, idx)
        m = np.isfinite(rp) & np.isfinite(rj)
        c = np.corrcoef(rp[m], rj[m])[0, 1] if m.sum() > 5 and rp[m].std() else 1.0
        d = float(np.max(np.abs(rp[m] - rj[m]))) if m.any() else 0.0
        worst_corr = min(worst_corr, c); worst_diff = max(worst_diff, d)

    n = len(prepped)
    print(f'genomes compared : {n}')
    print(f'worst corr       : {worst_corr:.10f}   (want ~1.0)')
    print(f'max |Δ return|   : {worst_diff:.3e}   (want < 1e-9)')
    print(f'numpy kernel     : {t_py / n * 1000:7.2f} ms/genome')
    print(f'numba kernel     : {t_jit / n * 1000:7.2f} ms/genome')
    print(f'speedup          : {t_py / t_jit:6.1f}x')
    ok = worst_corr > 1 - 1e-9 and worst_diff < 1e-9
    print('\nRESULT:', 'PASS — numba == numpy' if ok else 'FAIL — kernels diverge!')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
