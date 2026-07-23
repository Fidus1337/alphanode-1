"""Load search settings from config.ini (stdlib configparser, no external dependencies).

Returns a single cfg dict understood by evolve()/evaluate(). The same config is read by both
run_evo.py (search) and validate_champions.py (validation) — so that vol/fees/segments do not
diverge between stages.
"""
import os
import configparser
from datetime import datetime

import pandas as pd

try:
    from timeframe import resolve as _resolve_tf     # bar size -> annualization / grid / vol params
except Exception:                                    # pragma: no cover  (present in the shipped tree)
    _resolve_tf = None

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
# Paths can be overridden externally (the built application points to the bundle/user folder).
# Empty/unset — as before, next to the sources.
DEFAULT_INI = os.environ.get('ALPHANODE_CONFIG_INI') or os.path.join(HERE, 'config.ini')
DATA = os.environ.get('ALPHANODE_DATA') or os.path.join(PROJ, 'data.pickle')


def _ts(s):
    return pd.Timestamp(s.strip(), tz='UTC')


def load_config(path=None):
    path = path or DEFAULT_INI
    if not os.path.exists(path):
        raise FileNotFoundError(f'config not found: {path}')
    cp = configparser.ConfigParser(inline_comment_prefixes=('#', ';'))
    cp.read(path, encoding='utf-8')

    seg = cp['segments']
    jobs_raw = cp.get('search', 'jobs', fallback='auto').strip()
    jobs = max(1, (os.cpu_count() or 4) - 2) if jobs_raw.lower() == 'auto' else int(jobs_raw)

    uni_raw = cp.get('universe', 'instruments', fallback='all').strip()
    if uni_raw.lower() in ('', 'all', '*'):
        instruments = None                                  # None -> all from data.pickle
    else:
        instruments = [x.strip().upper() for x in uni_raw.replace('\n', ',').split(',') if x.strip()]

    tf_name = os.environ.get('ALPHANODE_TF') or cp.get('timeframe', 'tf', fallback='1d')
    if _resolve_tf is not None:
        _tf = _resolve_tf(tf_name)
        tf_fields = {'tf': _tf.name, 'ann': _tf.periods_per_year, 'freq': _tf.pandas_freq,
                     'vol_window': _tf.vol_window, 'ewma_lambda': _tf.ewma_lambda,
                     'binance_interval': _tf.binance_interval}
    else:                                            # daily fallback (identical to the original engine)
        tf_fields = {'tf': '1d', 'ann': 365.0, 'freq': 'D', 'vol_window': 30,
                     'ewma_lambda': 0.06, 'binance_interval': '1d'}

    # Per-timeframe data snapshot. An explicit ALPHANODE_DATA always wins (the GUI/workers pass
    # the right file); otherwise 1d keeps the historical data.pickle and intraday gets its own
    # data_<tf>.pickle — so switching timeframes never clobbers another timeframe's history.
    if os.environ.get('ALPHANODE_DATA'):
        data = DATA
    else:
        suffix = '' if tf_fields['tf'] == '1d' else f'_{tf_fields["tf"]}'
        data = os.path.join(PROJ, f'data{suffix}.pickle')

    return {
        'data': data,
        'instruments': instruments,
        **tf_fields,
        'start': datetime.fromisoformat(seg['train_start'].strip()),
        'end': datetime.fromisoformat(seg['test_end'].strip()),
        'splits': {
            'train': (_ts(seg['train_start']), _ts(seg['val_start'])),
            'val':   (_ts(seg['val_start']),   _ts(seg['test_start'])),
            'test':  (_ts(seg['test_start']),  _ts(seg['test_end'])),
        },
        'vol': cp.getfloat('simulation', 'target_vol'),
        'exec': cp.getfloat('simulation', 'exec_cost'),
        'pop': cp.getint('search', 'population'),
        'gens': cp.getint('search', 'generations'),
        'seed': cp.getint('search', 'seed'),
        'n_jobs': jobs,
        'max_depth': cp.getint('genome', 'max_depth'),
        'max_size': cp.getint('genome', 'max_size'),
        'tourn': cp.getint('ga', 'tournament'),
        'elitism': cp.getint('ga', 'elitism'),
        'random_inject': cp.getint('ga', 'random_inject'),
        'cx_prob': cp.getfloat('ga', 'crossover_prob'),
        'parsimony': cp.getfloat('fitness', 'parsimony'),
        'corr_thresh': cp.getfloat('fitness', 'corr_threshold'),
        'corr_penalty': cp.getfloat('fitness', 'corr_penalty'),
        'hof_cap': cp.getint('fitness', 'hof_capacity'),
    }
