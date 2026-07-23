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
    # --- recommended limits per bar size (finer bars: shorter history, fewer pairs) ---
    history: str = '2019-09-05'    # default fetch/search start (also TRAIN start)
    val_start: str = '2021-11-01'  # TRAIN | VAL boundary
    test_start: str = '2023-01-01'  # VAL | TEST boundary
    test_end: str = '2026-07-05'   # end of TEST (held-out)
    max_pairs: int = 150           # sane default universe size for downloads at this bar size

    @property
    def periods_per_day(self):
        return DAY_SECONDS / self.seconds

    @property
    def periods_per_year(self):
        return self.periods_per_day * ANN_DAYS

    @property
    def segments(self):
        """Recommended TRAIN/VAL/TEST dates for this bar size (train_start = history)."""
        return {'train_start': self.history, 'val_start': self.val_start,
                'test_start': self.test_start, 'test_end': self.test_end}


# Registry. vol_window ≈ 30 days of bars (keeps the "monthly vol" meaning of the daily rolling(30)).
# The history/segment windows shrink as bars get finer: intraday history on Binance perps is shorter,
# far denser, and heavier to download, so we trade calendar span for a comparable bar count and a
# reasonable download. Each window still spans several market regimes. Tune to taste in config.ini /
# the GUI date fields — these are only the defaults the timeframe selector fills in.
_TF = {
    '1d':  Timeframe('1d',  '1d',  'D',     86400,   30,
                     history='2019-09-05', val_start='2021-11-01', test_start='2023-01-01',
                     test_end='2026-07-05', max_pairs=150),
    '4h':  Timeframe('4h',  '4h',  '4h',    14400,  180,          # 30d * 6/day
                     history='2020-06-01', val_start='2023-01-01', test_start='2024-09-01',
                     test_end='2026-07-01', max_pairs=100),
    '1h':  Timeframe('1h',  '1h',  'h',      3600,  720,          # 30d * 24/day
                     history='2022-06-01', val_start='2024-06-01', test_start='2025-06-01',
                     test_end='2026-07-01', max_pairs=60),
    '15m': Timeframe('15m', '15m', '15min',   900, 2880,
                     history='2024-01-01', val_start='2025-04-01', test_start='2025-12-01',
                     test_end='2026-07-01', max_pairs=40),
    '5m':  Timeframe('5m',  '5m',  '5min',    300, 8640,
                     history='2024-09-01', val_start='2025-09-01', test_start='2026-03-01',
                     test_end='2026-07-01', max_pairs=25),
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
