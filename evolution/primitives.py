"""Primitive dictionary for genetic programming of alpha signals.

A strategy gene is a FORMULA that turns OHLCV into a per-instrument `alpha` signal.
Everything else (inverse-vol, normalization, vol-targeting, inertia, fees) is the
fixed quantpylib engine. So we evolve exactly this formula.

All operators work on WIDE tables (index=date, columns=tickers):
  * time-series operators run along axis 0 (within a ticker, past only -> no look-ahead);
  * cross-sectional operators run along axis 1 (a market cross-section on a date, as in XSMomentum).

This way cs-operations are trivial, and tickers missing on early dates (NaN) are correctly
ignored by rank/mean over the row.
"""
import numpy as np
import pandas as pd

EPS = 1e-9

# windows for time-series operators (the numeric "knobs" evolution tunes)
WINDOWS = [2, 3, 5, 7, 10, 14, 20, 30, 50, 60, 100, 120, 200]

# terminal features (tree leaves). Base OHLCV + ret, plus derived same-day transforms
# (vwap/range/body/dvol/logret) built in evaluator.add_derived_features.
FEATURES = ['close', 'open', 'high', 'low', 'volume', 'ret',
            'vwap', 'range', 'body', 'dvol', 'logret']


def _clean(df):
    """inf -> NaN (NaN is then ffilled/masked by eligible in the engine)."""
    return df.replace([np.inf, -np.inf], np.nan)


# ---------- binary (element-wise) ----------
def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b


def div(a, b):
    denom = b.where(b.abs() > EPS)          # guard against division by ~0
    return _clean(a / denom)


def pmin(a, b):
    a, b = a.align(b, join='outer')              # align by labels, not positionally
    return pd.DataFrame(np.minimum(a.values, b.values), index=a.index, columns=a.columns)


def pmax(a, b):
    a, b = a.align(b, join='outer')
    return pd.DataFrame(np.maximum(a.values, b.values), index=a.index, columns=a.columns)


def gt(a, b):
    a, b = a.align(b, join='outer')
    return (a > b).astype(float).where(a.notna() & b.notna())   # 1 where a>b else 0; NaN stays NaN


def lt(a, b):
    a, b = a.align(b, join='outer')
    return (a < b).astype(float).where(a.notna() & b.notna())


# ---------- unary element-wise ----------
def neg(a):
    return -a


def sign(a):
    return np.sign(a)


def absx(a):
    return a.abs()


def slog(a):
    return np.sign(a) * np.log1p(a.abs())    # signed log: defined everywhere, compresses tails


def tanhx(a):
    return np.tanh(a)


def sigmoid(a):
    return 1.0 / (1.0 + np.exp(-a)) - 0.5    # centered logistic squash -> (-0.5, 0.5)


def ssqrt(a):
    return np.sign(a) * np.sqrt(a.abs())     # signed square-root: compress tails, keep sign


# ---------- unary time-series (need a window; past only) ----------
def _mp(w):
    return max(2, w // 2)                     # min_periods: a bit softer than the full window


def ts_mean(a, w):
    return a.rolling(w, min_periods=_mp(w)).mean()


def ts_std(a, w):
    return a.rolling(w, min_periods=_mp(w)).std()


def ts_zscore(a, w):
    m = a.rolling(w, min_periods=_mp(w)).mean()
    s = a.rolling(w, min_periods=_mp(w)).std()
    return _clean((a - m) / s.where(s > EPS))  # = Bollinger when a=close, w=14


def ts_min(a, w):
    return a.rolling(w, min_periods=_mp(w)).min()


def ts_max(a, w):
    return a.rolling(w, min_periods=_mp(w)).max()


def ts_delta(a, w):
    return a - a.shift(w)


def ts_delay(a, w):
    return a.shift(w)


def ts_sum(a, w):
    return a.rolling(w, min_periods=_mp(w)).sum()


def ts_roc(a, w):
    prev = a.shift(w)
    return _clean(a / prev.where(prev.abs() > EPS) - 1)  # momentum / rate-of-change


def ema(a, w):
    return a.ewm(span=w, adjust=False).mean()


def ts_rank(a, w):
    return a.rolling(w, min_periods=_mp(w)).rank(pct=True) - 0.5   # percentile of today within its window


def ts_argmax(a, w):
    return a.rolling(w, min_periods=_mp(w)).apply(
        lambda x: len(x) - 1 - int(np.argmax(x)), raw=True)        # days since the window high (0 = today)


def ts_argmin(a, w):
    return a.rolling(w, min_periods=_mp(w)).apply(
        lambda x: len(x) - 1 - int(np.argmin(x)), raw=True)        # days since the window low


def ts_median(a, w):
    return a.rolling(w, min_periods=_mp(w)).median()


def ts_skew(a, w):
    return a.rolling(w, min_periods=_mp(w)).skew()


def ts_kurt(a, w):
    return a.rolling(w, min_periods=_mp(w)).kurt()


def decay_linear(a, w):
    wts = np.arange(1, w + 1, dtype=float)
    wts /= wts.sum()                                               # recent weighted more (min_periods = w)
    return a.rolling(w).apply(lambda x: float(np.dot(x, wts)), raw=True)


# ---------- binary time-series (two series + a window; past only) ----------
def ts_corr(a, b, w):
    a, b = a.align(b, join='outer')
    mp = _mp(w)
    ma = a.rolling(w, min_periods=mp).mean()
    mb = b.rolling(w, min_periods=mp).mean()
    cov = (a * b).rolling(w, min_periods=mp).mean() - ma * mb
    va = (a * a).rolling(w, min_periods=mp).mean() - ma * ma
    vb = (b * b).rolling(w, min_periods=mp).mean() - mb * mb
    denom = va * vb
    return _clean(cov / denom.where(denom > EPS) ** 0.5)           # rolling Pearson corr in [-1, 1]


def ts_cov(a, b, w):
    a, b = a.align(b, join='outer')
    mp = _mp(w)
    ma = a.rolling(w, min_periods=mp).mean()
    mb = b.rolling(w, min_periods=mp).mean()
    return _clean((a * b).rolling(w, min_periods=mp).mean() - ma * mb)


# ---------- cross-sectional (no window; a market slice on a date) ----------
def cs_rank(a):
    return a.rank(axis=1, pct=True) - 0.5     # centered rank across the market


def cs_zscore(a):
    m = a.mean(axis=1)
    s = a.std(axis=1)
    return _clean(a.sub(m, axis=0).div(s.where(s > EPS), axis=0))


def cs_demean(a):
    return a.sub(a.mean(axis=1), axis=0)


def cs_scale(a):
    s = a.abs().sum(axis=1)
    return _clean(a.div(s.where(s > EPS), axis=0))


# ---------- registry ----------
_FUNCS = {
    'add': add, 'sub': sub, 'mul': mul, 'div': div, 'pmin': pmin, 'pmax': pmax,
    'gt': gt, 'lt': lt,
    'neg': neg, 'sign': sign, 'abs': absx, 'slog': slog, 'tanh': tanhx,
    'sigmoid': sigmoid, 'ssqrt': ssqrt,
    'ts_mean': ts_mean, 'ts_std': ts_std, 'ts_zscore': ts_zscore, 'ts_min': ts_min,
    'ts_max': ts_max, 'ts_delta': ts_delta, 'ts_delay': ts_delay, 'ts_sum': ts_sum,
    'ts_roc': ts_roc, 'ema': ema,
    'ts_rank': ts_rank, 'ts_argmax': ts_argmax, 'ts_argmin': ts_argmin,
    'ts_median': ts_median, 'ts_skew': ts_skew, 'ts_kurt': ts_kurt, 'decay_linear': decay_linear,
    'ts_corr': ts_corr, 'ts_cov': ts_cov,
    'cs_rank': cs_rank, 'cs_zscore': cs_zscore, 'cs_demean': cs_demean, 'cs_scale': cs_scale,
}

BINARY = ['add', 'sub', 'mul', 'div', 'pmin', 'pmax', 'gt', 'lt']          # arity 2, no window
UN_ELEM = ['neg', 'sign', 'abs', 'slog', 'tanh', 'sigmoid', 'ssqrt']       # arity 1, no window
UN_TS = ['ts_mean', 'ts_std', 'ts_zscore', 'ts_min', 'ts_max',             # arity 1, window
         'ts_delta', 'ts_delay', 'ts_sum', 'ts_roc', 'ema',
         'ts_rank', 'ts_argmax', 'ts_argmin', 'ts_median', 'ts_skew', 'ts_kurt', 'decay_linear']
BIN_TS = ['ts_corr', 'ts_cov']                                             # arity 2, window
UN_CS = ['cs_rank', 'cs_zscore', 'cs_demean', 'cs_scale']                  # arity 1, no window

ALL_PRIMS = BINARY + UN_ELEM + UN_TS + BIN_TS + UN_CS
ARITY = {op: 2 for op in BINARY + BIN_TS}
ARITY.update({op: 1 for op in UN_ELEM + UN_TS + UN_CS})
NEEDS_WINDOW = {op: (op in UN_TS or op in BIN_TS) for op in ALL_PRIMS}

# compatibility groups for point mutation (swap an operator for one of the SAME arity+window kind)
COMPAT_GROUPS = [BINARY, UN_ELEM, UN_TS, BIN_TS, UN_CS]


def apply_primitive(op, args, window):
    """Apply a primitive to the already-computed arguments (a list of wide tables)."""
    f = _FUNCS[op]
    if NEEDS_WINDOW[op]:
        return _clean(f(*args, window))       # unary ts: f(a, w) · binary ts: f(a, b, w)
    return _clean(f(*args))
