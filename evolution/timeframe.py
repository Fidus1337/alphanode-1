"""Timeframe abstraction: everything that depends on the bar size (5m … 1d), in one place.

The engine (build_panel / precompute_market / fast_sim / metrics) is otherwise timeframe-agnostic,
so daily behaviour is reproduced EXACTLY by DAILY, and any intraday bar size is just a different
Timeframe resolved from config. Nothing else in the engine hard-codes "a day".

Derived quantities:
  * periods_per_year = (86400 / seconds) * 365   -> Sharpe = mean/std * sqrt(periods_per_year)
    (crypto trades 24/7, hence 365, not 252 trading days)
  * pandas_freq       -> the calendar grid build_panel reindexes onto
  * binance_interval  -> what fetch_data / the live paper loop request
  * vol_window        -> bars for the rolling realized-vol estimate (a knob; ~30 days of bars)
  * ewma_lambda       -> vol-target EWMA decay PER BAR (retune per tf so the wall-clock half-life
                         stays comparable; kept at the daily value for now)
"""
from dataclasses import dataclass

DAY_SECONDS = 86400
ANN_DAYS = 365                     # 24/7 market -> 365 calendar days, not 252 trading days


@dataclass(frozen=True)
class Timeframe:
    name: str
    binance_interval: str          # fetch_data + live paper klines
    pandas_freq: str               # build_panel reindex grid
    seconds: int
    vol_window: int                # bars for rolling vol estimation
    ewma_lambda: float = 0.06      # vol-target EWMA decay per bar

    @property
    def periods_per_day(self):
        return DAY_SECONDS / self.seconds

    @property
    def periods_per_year(self):
        return self.periods_per_day * ANN_DAYS


# Registry. vol_window ≈ 30 days of bars (keeps the "monthly vol" meaning of the daily rolling(30));
# it and ewma_lambda are knobs to retune once intraday runs are benchmarked.
_TF = {
    '1d':  Timeframe('1d',  '1d',  'D',     86400,   30),
    '4h':  Timeframe('4h',  '4h',  '4h',    14400,  180),   # 30d * 6/day
    '1h':  Timeframe('1h',  '1h',  'h',      3600,  720),   # 30d * 24/day
    '15m': Timeframe('15m', '15m', '15min',   900, 2880),
    '5m':  Timeframe('5m',  '5m',  '5min',    300, 8640),
}

DAILY = _TF['1d']


def resolve(name):
    """Timeframe by short name ('5m','15m','1h','4h','1d'); default/blank -> daily."""
    key = (name or '1d').strip().lower()
    if key not in _TF:
        raise ValueError(f'unknown timeframe {name!r}; known: {list(_TF)}')
    return _TF[key]


def known():
    return list(_TF)
