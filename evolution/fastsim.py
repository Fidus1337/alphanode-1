"""A fast numpy port of the run_simulation engine (for the evolution fitness).

Semantics 1-to-1 with quantpylib/simulator/alpha.py:
  * the same vol-targeting via strat_scalar + EWMA(0.06) of realized volatility;
  * the same inverse-vol normalization of forecasts and forecast_chips;
  * the same position inertia (10% no-trade band) and fees on turnover;
  * eligible/vol/ret are built exactly as in the engine (ffill+bfill close, floor 0.005, etc.).

The market matrices (C,R,V,base_elig) are computed ONCE; only the alpha matrix changes per
genome -> the daily loop runs over numpy vectors (30 instruments), not pandas .at[].
Result: ~0.05s instead of ~32s per genome. Agreement with the engine is checked in verify_fastsim.py.
"""
import numpy as np
import pandas as pd

VOL_FLOOR = 0.005
EWMA_LAMBDA = 0.06
TARGET_ANN = 365


def precompute_market(panel, tk, raw=None):
    """Constant market matrices [T, N] (the same for all genomes).

    raw (the raw daily dfs per ticker) — to compute vol exactly like the engine: on the NATIVE
    close before reindex, then aligned to the common calendar and ffill (alpha.py:54).

    panel['close'] arrives ffill ONLY (no bfill — a cs-leak guard). For the simulation matrices
    (price/ret/eligible) the engine bfills close, so we bfill here: on live dates it changes
    nothing, and the pre-listing flat region is correctly not-eligible."""
    close = panel['close'][tk].bfill()              # engine-correct bfilled close for C/R/eligible
    C = close.to_numpy(dtype=np.float64)
    prev = close.shift(1)
    R = (close / prev - 1.0).to_numpy(dtype=np.float64)
    R[0, :] = 0.0
    R = np.nan_to_num(R, nan=0.0)

    if raw is not None:                             # vol as in the engine: on the native close
        vcols = {}
        for t in tk:
            nc = raw[t]['close']
            vcols[t] = ((-1 + nc / nc.shift(1)).rolling(30).std()).reindex(close.index)
        V = pd.DataFrame(vcols)[tk].ffill().fillna(0.0).to_numpy(dtype=np.float64)
    else:                                           # fallback: on the reindexed close
        V = close.pct_change().rolling(30).std().ffill().fillna(0.0).to_numpy(dtype=np.float64)
    V = np.where(V < VOL_FLOOR, VOL_FLOOR, V)

    sampled = (close != close.shift(1)).fillna(False).astype(float)
    base = sampled.rolling(5).max().fillna(0.0).to_numpy()   # any() over 5 days == max for 0/1
    base_elig = base > 0
    idx = close.index
    return {'C': C, 'R': R, 'V': V, 'base_elig': base_elig, 'index': idx, 'tk': list(tk)}


def fast_sim(alpha_values, market, vol_target=0.30, exec_rate=0.001, inertia=0.10):
    """alpha_values: [T, N] raw signal (in market['tk'] order); NaN where there's no data.
    Returns a pd.Series of NET capital returns (like capital.pct_change() in the engine)."""
    C, R, V, base_elig = market['C'], market['R'], market['V'], market['base_elig']
    T, N = C.shape

    A = pd.DataFrame(alpha_values).ffill().to_numpy(dtype=np.float64)   # post_compute ffill of the signal
    E = base_elig & np.isfinite(A)                                      # eligible &= ~isna(alpha)

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
            strat_scalar = ewstrat * vol_target / np.sqrt(ewma * TARGET_ANN)
            dprice = C[i] - C[i - 1]
            day_pnl = np.nansum(units_prev * dprice)
            nominal_ret = np.nansum(w_prev * R[i])
            capital_ret = nominal_ret * lev_prev
            cap = capital[i - 1] + day_pnl
            if capital_ret != 0:                       # the engine freezes EWMA on "dead" days
                ewma = EWMA_LAMBDA * capital_ret ** 2 + (1 - EWMA_LAMBDA) * ewma
                ewstrat = EWMA_LAMBDA * strat_scalar + (1 - EWMA_LAMBDA) * ewstrat

        elig = E[i]
        Ci = C[i]
        fc = np.where(elig, A[i], 0.0) / V[i]
        fc = np.where(elig, fc, 0.0)
        chips = np.nansum(np.abs(fc))
        scaled = fc / chips if chips != 0 else np.zeros(N)
        pos_raw = strat_scalar * scaled * cap / Ci

        change = np.where(elig, pos_raw - pos_prev, 0.0)
        with np.errstate(divide='ignore', invalid='ignore'):
            pct = np.abs(change) / np.abs(pos_raw)     # nan/inf when pos_raw≈0 -> hold=False (as in the engine)
        hold = elig & (pct < inertia)
        position = np.where(hold, pos_prev, np.where(elig, pos_raw, 0.0))
        change = np.where(hold, 0.0, change)

        costs = np.nansum(np.where(elig, np.abs(change) * Ci * exec_rate, 0.0))
        nominal_tot = np.nansum(np.abs(position * Ci))
        w = (position * Ci / nominal_tot) if nominal_tot != 0 else np.zeros(N)

        cap -= costs
        capital[i] = cap
        lev_prev = nominal_tot / cap if cap != 0 else 0.0
        units_prev = position
        w_prev = w
        pos_prev = position

    ser = pd.Series(capital, index=market['index'])
    return ser.pct_change().fillna(0.0)
