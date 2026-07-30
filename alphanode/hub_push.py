"""Live target weights for an AlphaHub push — ANY timeframe the search supports.

Two engines, chosen by timeframe — deliberately:

* **1d** -> the REAL quantpylib engine (Alpha + Portfolio), exactly the numbers the
  Serve/paper/portfolio products produce. A daily hub track therefore matches a paper
  run bar for bar.
* **intraday** -> evolution's fastsim, which is timeframe-parameterized (ann /
  vol_window / ewma_lambda from timeframe.py). This is the SAME engine whose numbers
  the search leaderboard shows, so an intraday hub track means what the search meant.
  (The two engines agree on direction but not to the digit: Portfolio adds a second
  vol-targeting + inertia layer on top of Alpha, so weights differ by a few percent.)

Pipeline for intraday, all on the node:

    fresh klines (+ funding history) from Binance  ->  panel_from_raw  ->  eval formula
    ->  fast_sim_weights  ->  {symbol: weight} on the LAST closed bar

The funding FEATURE is fetched too (public /fapi/v1/fundingRate), so formulas that use
`funding` see live values, not zeros.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
for _p in (os.path.join(PROJ, 'evolution'), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd                                      # noqa: E402

FUNDING = 'https://fapi.binance.com/fapi/v1/fundingRate'
TF_SEC = {'15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
BARS = 900                                               # history depth: max window 500 + warmup


def next_close(tf_name, now=None, margin=60):
    """Unix ts of the first tf-aligned close at least `margin` seconds away — so a push
    computed just before a close doesn't arrive a second too late (403)."""
    step = TF_SEC[tf_name]
    now = now if now is not None else time.time()
    return (int(now + margin) // step + 1) * step


def _fetch_funding(symbol, start_ms, timeout=20):
    """Funding payments since start_ms -> Series(rate @ fundingTime). Public, no key."""
    out, cur = [], start_ms
    for _ in range(8):                                   # 1000 payments/page = 333 days each
        q = urllib.parse.urlencode({'symbol': symbol, 'startTime': cur, 'limit': 1000})
        with urllib.request.urlopen(f'{FUNDING}?{q}', timeout=timeout) as r:
            rows = json.loads(r.read())
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 1000:
            break
        cur = int(rows[-1]['fundingTime']) + 1
    if not out:
        return pd.Series(dtype=float)
    ser = pd.Series({pd.Timestamp(int(r['fundingTime']), unit='ms', tz='UTC'):
                     float(r['fundingRate']) for r in out})
    return ser[~ser.index.duplicated()].sort_index()


def _attach_funding(df, fser, freq):
    """Funding paid WITHIN each bar = sum of the 8h payments that fall inside it (a flow,
    same convention as fetch_data / the search snapshots)."""
    if fser.empty:
        df['funding'] = 0.0
        return df
    per_bar = fser.groupby(fser.index.floor(freq)).sum()
    df['funding'] = per_bar.reindex(df.index).fillna(0.0)
    return df


def compute_weights(formula, tickers, tf_name, vol, exec_rate, log=print):
    """-> (weights dict, meta dict). 1d = real engine (paper parity); intraday = fastsim."""
    from timeframe import resolve
    from evaluator import panel_from_raw, make_market, eval_alpha_panel
    from fastsim import fast_sim_weights
    from genome import parse
    import signal_service

    tf = resolve(tf_name)
    if tf.name == '1d':                                  # the daily products' engine, verbatim
        now = time.time()
        start = datetime.fromtimestamp(now - BARS * 86400, tz=timezone.utc)
        sig = signal_service.compute_signal([formula], tickers,
                                            start.replace(tzinfo=None), vol, exec_rate)
        weights = {p['ticker']: p['weight'] for p in sig['positions']}
        return weights, {'as_of': sig['as_of'], 'leverage': sig['leverage'],
                         'n_assets': sig['n_assets'], 'tf': '1d'}
    step = TF_SEC[tf.name]
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - BARS * step * 1000

    node = parse(formula)
    dfs = {}
    for t in tickers:
        try:
            df = signal_service.fetch_klines(t, start_ms, now_ms, interval=tf.binance_interval)
            if len(df) > tf.vol_window + 60:             # enough bars to matter
                dfs[t] = _attach_funding(df, _fetch_funding(t, start_ms), tf.pandas_freq)
        except Exception as e:                           # noqa: BLE001 — skip the ticker, keep going
            log(f'hub_push: {t} skipped ({type(e).__name__})')
    if len(dfs) < 2:
        raise RuntimeError(f'live data for only {len(dfs)} ticker(s) — cannot build a cross-section')

    tk = list(dfs)
    start = min(df.index[0] for df in dfs.values())
    end = max(df.index[-1] for df in dfs.values())
    panel = panel_from_raw(tk, dfs, start, end, freq=tf.pandas_freq)
    market = make_market(panel, tk, dfs, vol_window=tf.vol_window)
    alpha = eval_alpha_panel(node, panel)
    weights, lev = fast_sim_weights(alpha[tk].to_numpy(dtype=float), market, vol, exec_rate,
                                    ann=tf.periods_per_year, ewma_lambda=tf.ewma_lambda)
    as_of = end.strftime('%Y-%m-%d %H:%M')
    return weights, {'as_of': as_of, 'leverage': round(lev, 4), 'n_assets': len(tk),
                     'tf': tf.name}


def bar_close_iso(tf_name, now=None):
    return datetime.fromtimestamp(next_close(tf_name, now), tz=timezone.utc)\
        .strftime('%Y-%m-%dT%H:%M:%SZ')
