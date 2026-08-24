"""Direction regime (trend up/down/flat) — which formula earns in which market mode,
measured on TEST only. The gate is a drift t-statistic, NOT R² of
price-on-time (spurious regression: ~62% of driftless chop reads as 'trend' there),
and consumers slice returns with ONE-BAR-LAGGED labels (a label's window must not
contain the return it conditions). Each of those was a confirmed review finding —
the tests below pin them."""
import configparser
import os

import numpy as np
import pandas as pd

from evaluator import trend_regime

W = 30
IDX = pd.date_range('2024-01-01', periods=300, freq='D', tz='UTC')


def _panel(prices):
    prices = pd.DataFrame(np.asarray(prices, dtype=np.float64), index=IDX[:len(prices)])
    return {'close': prices, 'ret': prices.pct_change().fillna(0.0)}


def _steady(drift, noise, n=300, seed=7):
    rng = np.random.default_rng(seed)
    r = drift + noise * rng.standard_normal((n, 2))
    return 100.0 * np.exp(np.cumsum(r, axis=0))


def test_uptrend_reads_up():
    lab = trend_regime(_panel(_steady(+0.01, 0.002)), window=W)
    assert (lab.iloc[W:] == 1.0).mean() > 0.9


def test_downtrend_reads_down():
    lab = trend_regime(_panel(_steady(-0.01, 0.002)), window=W)
    assert (lab.iloc[W:] == -1.0).mean() > 0.9


def test_driftless_noise_reads_flat_across_seeds():
    """The R² gate died here: population flat share was ~0.38 and the old assert passed
    on ~1 seed in 10. The t-stat gate's iid-null directional share is ~21%, so flat ≈ 79%
    IN EXPECTATION — asserted across 12 seeds, not one lucky one."""
    shares = []
    for seed in range(12):
        lab = trend_regime(_panel(_steady(0.0, 0.02, seed=seed)), window=W)
        shares.append(float((lab.iloc[W:] == 0.0).mean()))
    assert np.mean(shares) > 0.7
    assert min(shares) > 0.4                             # no seed collapses to mostly-directional


def test_warmup_is_nan_then_labels_begin():
    lab = trend_regime(_panel(_steady(+0.01, 0.002)), window=W)
    assert lab.iloc[:W - 1].isna().all()
    assert lab.iloc[W:].notna().all()


def test_causal_prefix_never_changes_when_future_arrives():
    full_prices = _steady(0.0, 0.015, seed=3)
    full = trend_regime(_panel(full_prices), window=W)
    pref = trend_regime(_panel(full_prices[:200]), window=W)
    a, b = full.iloc[:200], pref
    assert ((a == b) | (a.isna() & b.isna())).all()      # labels are history, not hindsight


def test_dead_flat_price_is_flat_not_crash():
    lab = trend_regime(_panel(np.full((120, 2), 100.0)), window=W)
    assert (lab.iloc[W:] == 0.0).all()


def test_noiseless_steady_drift_is_a_trend():
    """sd == 0 with mu != 0 → t = inf → labeled: a perfectly steady climb IS a trend."""
    prices = 100.0 * np.exp(np.cumsum(np.full((120, 2), 0.01), axis=0))
    lab = trend_regime(_panel(prices), window=W)
    assert (lab.iloc[W:] == 1.0).all()


def test_one_poisoned_bar_recovers_instead_of_killing_the_series():
    """Review finding: a single zero close made ret=inf, the old cumsum carried it to the
    end and EVERY later label was NaN (all three columns silently dashed). Cleaned input
    must recover once the bad bar leaves the window."""
    prices = _steady(+0.005, 0.002)
    prices[100, 0] = 0.0                                 # one dead print in one asset
    lab = trend_regime(_panel(prices), window=W)
    assert lab.iloc[100 + W + 2:].notna().all()          # alive again after the window passes


def test_worker_lags_labels_and_keeps_buckets_alive_on_real_data():
    """End-to-end on the repo's data.pickle: build_ctx must hand trade_stats LAGGED labels
    (bar t = regime known at t-1), and at t_hi=1.28 all three TEST buckets stay usable
    (the review killed t_hi≈2: the down bucket shrank to 3 bars = permanent dash)."""
    import metrics_worker as mw
    cp = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    cp.read(os.environ['ALPHANODE_CONFIG_INI'])
    seg = cp['segments']
    ctx = mw.build_ctx({'formulas': ['tanh(ret)'],
                        'train_start': seg['train_start'].strip(),
                        'test_start': seg['test_start'].strip(),
                        'test_end': seg['test_end'].strip()})
    raw = trend_regime(ctx['panel'], window=30).reindex(
        pd.DatetimeIndex(ctx['market']['index'])).to_numpy()
    got, want = ctx['trend'][1:], raw[:-1]
    assert np.isnan(ctx['trend'][0])
    assert (((got == want) | (np.isnan(got) & np.isnan(want)))).all()   # exactly one bar of lag
    bars = mw.trend_bar_counts(ctx)                      # what the header '·N' shows
    assert bars['up'] > 100 and bars['down'] > 40 and bars['flat'] > 600
    r = mw.trade_stats('tanh(ret)', ctx)
    assert isinstance(r, dict)
    for k in ('tup', 'tdown', 'tflat'):
        assert isinstance(r[k], float)                   # ...so none of them dashes


def test_trend_split_maps_buckets_to_the_right_keys():
    """Mutation check from the review: swapping the +1/-1 slices kept the suite green.
    Now a bucket-key swap flips a sign here."""
    import metrics_worker as mw
    rng = np.random.default_rng(5)
    trd = np.array([np.nan] * 10 + [1.0] * 60 + [-1.0] * 60 + [0.0] * 60)
    rt = np.concatenate([rng.normal(0.05, 0.001, 10),    # NaN bars: loud but must be ignored
                         rng.normal(+0.01, 0.001, 60),
                         rng.normal(-0.01, 0.001, 60),
                         rng.normal(0.0, 0.001, 60)])
    s = mw.trend_split(rt, trd, ann=365.0)
    assert s['tup'] > 0 and s['tdown'] < 0
    assert abs(s['tflat']) < abs(s['tup'])
    thin = mw.trend_split(rt[:40], trd[:40], ann=365.0)
    assert thin['tdown'] is None and thin['tflat'] is None   # under 30 bars -> honest None


def test_gui_headers_carry_the_bucket_sizes(gui_app):
    """'T ↑ ·196': the sample size lives in the HEADER (one split for every row), and a
    theme rebuild must not lose it."""
    app, rec, state = gui_app
    app._trend_bars = {'up': 196, 'down': 81, 'flat': 1005}
    app._apply_trend_bars()
    assert '·196' in app.tree.heading('tup')['text']
    assert '·81' in app.tree.heading('tdown')['text']
    assert '·1005' in app.tree.heading('tflat')['text']
    assert '196' in app._HEAD_TIP['tup']
    app._apply_trend_bars()                              # idempotent — no double suffix
    assert app.tree.heading('tup')['text'].count('·196') == 1
    assert app._HEAD_TIP['tup'].count('196') == 1


def test_gui_trend_columns_render_from_the_cache(gui_app):
    app, rec, state = gui_app
    champ = {'formula': 'tanh(high)', 'base': 1.0, 'test': {'sharpe': 0.5}}
    app._metrics_cache[champ['formula']] = {
        'long': 3, 'short': 1, 'win': 0.5, 'act': 2.0, 'dd': -0.1, 'cagr': 0.2,
        'sortino': 1.0, 'tup': 1.234, 'tdown': -0.456, 'tflat': None}
    app._treesig = None
    app._fill_tree([champ])
    item = app.tree.get_children()[0]
    assert app.tree.set(item, 'tup') == '+1.23'
    assert app.tree.set(item, 'tdown') == '-0.46'
    assert app.tree.set(item, 'tflat') == '—'            # thin bucket stays an honest dash
    for c in ('tup', 'tdown', 'tflat'):
        assert c in app.tree['displaycolumns']           # visible out of the box
        assert c in app._SORTABLE
    assert not app._test_tk_errors
