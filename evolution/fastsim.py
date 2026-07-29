"""A fast numpy port of the run_simulation engine (for the evolution fitness).

Semantics 1-to-1 with quantpylib/simulator/alpha.py:
  * the same vol-targeting via strat_scalar + EWMA(0.06) of realized volatility;
  * the same inverse-vol normalization of forecasts and forecast_chips;
  * the same position inertia (10% no-trade band) and fees on turnover;
  * eligible/vol/ret are built exactly as in the engine (ffill+bfill close, floor 0.005, etc.).

The market matrices (C,R,V,base_elig) are computed ONCE; only the alpha matrix changes per
genome -> the daily loop runs over numpy vectors (30 instruments), not pandas .at[].

The day loop is the hot path (~86% of a genome's eval). It is compiled with numba when available
(~20x on the loop -> ~5-6x per genome); without numba it runs the IDENTICAL pure-numpy loop
(same numbers, current speed). Agreement with the real engine is checked in verify_fastsim.py;
numba==numpy is checked in verify_numba.py.
"""
import numpy as np
import pandas as pd

try:                                     # optional: compile the day loop to machine code
    import numba as _numba
except Exception:                        # noqa: BLE001 — numba is optional; any import error -> fallback
    _numba = None

# division-by-~0 in the loop is intentional (nan/inf -> hold=False); suppress the warnings for the
# pure-numpy fallback path (numba njit emits none). Matches evaluator.py's process-wide policy.
np.seterr(divide='ignore', invalid='ignore')

VOL_FLOOR = 0.005
EWMA_LAMBDA = 0.06
TARGET_ANN = 365


def precompute_market(panel, tk, raw=None, vol_window=30):
    """Constant market matrices [T, N] (the same for all genomes).

    raw (the raw daily dfs per ticker) — to compute vol exactly like the engine: on the NATIVE
    close before reindex, then aligned to the common calendar and ffill (alpha.py:54).

    panel['close'] arrives ffill ONLY (no bfill — a cs-leak guard). For the simulation matrices
    (price/ret/eligible) the engine bfills close, so we bfill here: on live dates it changes
    nothing, and the pre-listing flat region is correctly not-eligible."""
    close = panel['close'][tk].bfill()              # engine-correct bfilled close for C/R/eligible
    C = close.to_numpy(dtype=np.float64)
    prev = close.shift(1)
    # nan_to_num returns a fresh WRITABLE array — important under pandas Copy-on-Write (pandas 3.x
    # default), where .to_numpy() hands back a read-only view and R[0, :] = 0.0 would otherwise raise
    # "assignment destination is read-only".
    R = np.nan_to_num((close / prev - 1.0).to_numpy(dtype=np.float64), nan=0.0)
    R[0, :] = 0.0                                    # first row: no previous close

    if raw is not None:                             # vol as in the engine: on the native close
        vcols = {}
        for t in tk:
            nc = raw[t]['close']
            vcols[t] = ((-1 + nc / nc.shift(1)).rolling(vol_window).std()).reindex(close.index)
        V = pd.DataFrame(vcols)[tk].ffill().fillna(0.0).to_numpy(dtype=np.float64)
    else:                                           # fallback: on the reindexed close
        V = close.pct_change().rolling(vol_window).std().ffill().fillna(0.0).to_numpy(dtype=np.float64)
    V = np.where(V < VOL_FLOOR, VOL_FLOOR, V)

    sampled = (close != close.shift(1)).fillna(False).astype(float)
    base = sampled.rolling(5).max().fillna(0.0).to_numpy()   # any() over 5 days == max for 0/1
    base_elig = base > 0
    idx = close.index

    # funding paid during each bar (perps: longs pay when positive). Snapshots fetched before
    # funding support have no such panel -> zero matrix, i.e. the old price-only PnL exactly.
    if 'funding' in panel:
        F = panel['funding'][tk].fillna(0.0).to_numpy(dtype=np.float64)
    else:
        F = np.zeros_like(C)
    return {'C': C, 'R': R, 'V': V, 'F': F, 'base_elig': base_elig, 'index': idx, 'tk': list(tk)}


def _sim_kernel_impl(A, C, R, V, E, F, vol_target, exec_rate, inertia, ann, ewma_lambda, out):
    """The sequential day loop -> capital[T]. `out` (N+1 floats) additionally receives the
    LAST bar's dollar weights [0:N] and leverage [N] — the live target for AlphaHub pushes.

    State (EWMA vol estimate, capital, positions) carries across days, so this loop is inherently
    sequential and cannot be vectorized over time — which is exactly why it is worth compiling.
    Written in plain numpy so numba can njit it verbatim and the fallback stays byte-for-byte the
    old engine. `A` is the ffilled signal [T,N], `E` the eligibility mask [T,N]; `F` the funding
    rate paid during bar i (position held into the bar pays units*price*rate; zero matrix = old
    price-only PnL); `ann` = bars/year (vol-target annualization) and `ewma_lambda` = vol-EWMA
    decay per bar (both timeframe-dependent)."""
    T, N = C.shape
    capital = np.empty(T)
    units_prev = np.zeros(N)
    w_prev = np.zeros(N)
    pos_prev = np.zeros(N)
    lev_prev = 0.0
    ewma = 0.01
    ewstrat = 1.0

    for i in range(T):
        if i == 0:
            strat_scalar = 1.0
            cap = 10_000.0
        else:
            strat_scalar = ewstrat * vol_target / np.sqrt(ewma * ann)
            dprice = C[i] - C[i - 1]
            day_pnl = np.nansum(units_prev * dprice)
            # funding on the position held into this bar; notional at the last known price.
            # NOT fed into the vol-target EWMA (capital_ret) — that tracks price vol, engine-style.
            fund_pnl = -np.nansum(units_prev * C[i - 1] * F[i])
            nominal_ret = np.nansum(w_prev * R[i])
            capital_ret = nominal_ret * lev_prev
            cap = capital[i - 1] + day_pnl + fund_pnl
            if capital_ret != 0:                       # the engine freezes EWMA on "dead" days
                ewma = ewma_lambda * capital_ret ** 2 + (1 - ewma_lambda) * ewma
                ewstrat = ewma_lambda * strat_scalar + (1 - ewma_lambda) * ewstrat

        elig = E[i]
        Ci = C[i]
        fc = np.where(elig, A[i], 0.0) / V[i]
        fc = np.where(elig, fc, 0.0)
        chips = np.nansum(np.abs(fc))
        if chips != 0:
            scaled = fc / chips
        else:
            scaled = np.zeros(N)
        pos_raw = strat_scalar * scaled * cap / Ci

        change = np.where(elig, pos_raw - pos_prev, 0.0)
        pct = np.abs(change) / np.abs(pos_raw)         # nan/inf when pos_raw≈0 -> hold=False (as in the engine)
        hold = elig & (pct < inertia)
        position = np.where(hold, pos_prev, np.where(elig, pos_raw, 0.0))
        change = np.where(hold, 0.0, change)

        costs = np.nansum(np.where(elig, np.abs(change) * Ci * exec_rate, 0.0))
        nominal_tot = np.nansum(np.abs(position * Ci))
        if nominal_tot != 0:
            w = position * Ci / nominal_tot
        else:
            w = np.zeros(N)

        cap -= costs
        capital[i] = cap
        lev_prev = nominal_tot / cap if cap != 0 else 0.0
        units_prev = position
        w_prev = w
        pos_prev = position

    out[:N] = w_prev
    out[N] = lev_prev
    return capital


# Compile the kernel once (lazily, on first call). If numba is missing or can't handle this build
# (unsupported op, a read-only cache dir in a frozen bundle), we fall back permanently to the
# identical pure-numpy loop — so correctness never depends on numba being present.
_kernel_jit = _numba.njit(cache=True)(_sim_kernel_impl) if _numba is not None else None


def _run_kernel(A, C, R, V, E, F, vol_target, exec_rate, inertia, ann, ewma_lambda, out=None):
    if out is None:
        out = np.zeros(C.shape[1] + 1)
    global _kernel_jit
    if _kernel_jit is not None:
        try:
            return _kernel_jit(A, C, R, V, E, F, vol_target, exec_rate, inertia, ann,
                               ewma_lambda, out)
        except Exception:                              # noqa: BLE001 — any numba failure -> numpy
            _kernel_jit = None
    return _sim_kernel_impl(A, C, R, V, E, F, vol_target, exec_rate, inertia, ann,
                            ewma_lambda, out)


def fast_sim(alpha_values, market, vol_target=0.30, exec_rate=0.001, inertia=0.10,
             ann=TARGET_ANN, ewma_lambda=EWMA_LAMBDA):
    """alpha_values: [T, N] raw signal (in market['tk'] order); NaN where there's no data.
    `ann` = bars/year (vol-target annualization), `ewma_lambda` = vol-EWMA decay per bar (timeframe).
    Returns a pd.Series of NET capital returns (like capital.pct_change() in the engine)."""
    C, R, V, base_elig = market['C'], market['R'], market['V'], market['base_elig']
    F = market.get('F')
    if F is None:                                       # market dicts built before funding support
        F = np.zeros_like(C)
    A = pd.DataFrame(alpha_values).ffill().to_numpy(dtype=np.float64)   # post_compute ffill of the signal
    E = base_elig & np.isfinite(A)                                      # eligible &= ~isna(alpha)
    capital = _run_kernel(A, C, R, V, E, F, float(vol_target), float(exec_rate), float(inertia),
                          float(ann), float(ewma_lambda))
    ser = pd.Series(capital, index=market['index'])
    return ser.pct_change().fillna(0.0)


def fast_sim_weights(alpha_values, market, vol_target=0.30, exec_rate=0.001, inertia=0.10,
                     ann=TARGET_ANN, ewma_lambda=EWMA_LAMBDA):
    """Target DOLLAR weights on the LAST bar (sum(|w|)=1; longs>0, shorts<0) + leverage —
    the live payload for an AlphaHub push. Same kernel as fast_sim, so any timeframe the
    search supports is supported here (ann/ewma_lambda come from timeframe.py)."""
    C, R, V, base_elig = market['C'], market['R'], market['V'], market['base_elig']
    F = market.get('F')
    if F is None:
        F = np.zeros_like(C)
    A = pd.DataFrame(alpha_values).ffill().to_numpy(dtype=np.float64)
    E = base_elig & np.isfinite(A)
    out = np.zeros(C.shape[1] + 1)
    _run_kernel(A, C, R, V, E, F, float(vol_target), float(exec_rate), float(inertia),
                float(ann), float(ewma_lambda), out)
    weights = {t: float(out[i]) for i, t in enumerate(market['tk']) if abs(out[i]) > 1e-9}
    return weights, float(out[-1])
