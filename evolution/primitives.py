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
WINDOWS = [3, 5, 10, 14, 20, 30, 50, 100]

# terminal features (tree leaves)
FEATURES = ['close', 'open', 'high', 'low', 'volume', 'ret']


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
    'neg': neg, 'sign': sign, 'abs': absx, 'slog': slog, 'tanh': tanhx,
    'ts_mean': ts_mean, 'ts_std': ts_std, 'ts_zscore': ts_zscore, 'ts_min': ts_min,
    'ts_max': ts_max, 'ts_delta': ts_delta, 'ts_delay': ts_delay, 'ts_sum': ts_sum,
    'ts_roc': ts_roc, 'ema': ema,
    'cs_rank': cs_rank, 'cs_zscore': cs_zscore, 'cs_demean': cs_demean, 'cs_scale': cs_scale,
}

BINARY = ['add', 'sub', 'mul', 'div', 'pmin', 'pmax']
UN_ELEM = ['neg', 'sign', 'abs', 'slog', 'tanh']
UN_TS = ['ts_mean', 'ts_std', 'ts_zscore', 'ts_min', 'ts_max',
         'ts_delta', 'ts_delay', 'ts_sum', 'ts_roc', 'ema']
UN_CS = ['cs_rank', 'cs_zscore', 'cs_demean', 'cs_scale']

ALL_PRIMS = BINARY + UN_ELEM + UN_TS + UN_CS
ARITY = {op: (2 if op in BINARY else 1) for op in ALL_PRIMS}
NEEDS_WINDOW = {op: (op in UN_TS) for op in ALL_PRIMS}

# compatibility groups for point mutation (replace an operator with "one of the same type")
COMPAT_GROUPS = [BINARY, UN_ELEM, UN_TS, UN_CS]


def apply_primitive(op, args, window):
    """Apply a primitive to the already-computed arguments (a list of wide tables)."""
    f = _FUNCS[op]
    if NEEDS_WINDOW[op]:
        return _clean(f(args[0], window))
    return _clean(f(*args))
