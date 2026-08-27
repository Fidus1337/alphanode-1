"""The steadiness strip: twelve slices of history instead of one number.

A single TEST Sharpe cannot separate a formula that worked all along from one carried by
a single quarter. These tests build both, confirm the headline prefers the wrong one, and
confirm the strip does not.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, 'evolution'), os.path.join(ROOT, 'alphanode'), ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import metrics_worker as MW                                          # noqa: E402

ANN = 365.0


def _sharpe(r):
    return float(r.mean()) * ANN / (float(r.std()) * np.sqrt(ANN))


def _index(n, freq='D'):
    return pd.date_range('2020-01-01', periods=n, freq=freq, tz='UTC')


# ---------------- the slicing ----------------
def test_buckets_cut_equal_calendar_time():
    idx = _index(1200)
    b = MW.calendar_buckets(idx, 12)
    assert b.min() == 0 and b.max() == 11                # every slice used, none overflows
    counts = [int((b == k).sum()) for k in range(12)]
    assert sum(counts) == 1200
    assert max(counts) - min(counts) <= 1                # an even grid cuts evenly


def test_buckets_are_equal_time_not_equal_bars():
    """The point of cutting by calendar: a stretch the exchange served thinly stays a
    thin slice. Cutting by bar count instead would silently compare a fat quarter with a
    thin one and read the difference as performance."""
    dense = pd.date_range('2020-01-01', periods=600, freq='h', tz='UTC')      # 25 days
    sparse = pd.date_range('2020-01-26', periods=25, freq='D', tz='UTC')      # 25 days
    idx = dense.append(sparse)
    b = MW.calendar_buckets(idx, 2)
    assert int((b == 0).sum()) > 500                     # first half: the dense stretch
    assert int((b == 1).sum()) < 100                     # second half: the same TIME, fewer bars


def test_every_bar_lands_in_a_slice_including_the_last():
    idx = _index(100)
    b = MW.calendar_buckets(idx, 12)
    assert b.shape == (100,)
    assert b[0] == 0 and b[-1] == 11                     # the final bar must not fall off the end


def test_a_single_bar_history_does_not_divide_by_zero():
    b = MW.calendar_buckets(_index(1), 12)
    assert b.tolist() == [0]


# ---------------- the strip ----------------
def test_strip_returns_one_number_per_slice():
    rng = np.random.default_rng(4)
    rt = rng.normal(0.001, 0.01, 1200)
    b = MW.calendar_buckets(_index(1200), 12)
    strip = MW.stability_strip(rt, b, ANN)
    assert len(strip) == 12
    assert all(v is None or isinstance(v, float) for v in strip)


def test_a_thin_slice_reports_nothing_rather_than_a_number():
    """Under 30 bars there is no Sharpe worth printing — the same evidence floor the
    direction columns already use."""
    rng = np.random.default_rng(5)
    rt = rng.normal(0.0, 0.01, 240)                      # 240 bars over 12 slices = 20 each
    b = MW.calendar_buckets(_index(240), 12)
    assert MW.stability_strip(rt, b, ANN) == [None] * 12


def test_a_slice_the_formula_sat_out_reports_nothing():
    rng = np.random.default_rng(6)
    rt = rng.normal(0.001, 0.01, 1200)
    b = MW.calendar_buckets(_index(1200), 12)
    rt[b == 3] = 0.0                                     # flat through slice 3
    strip = MW.stability_strip(rt, b, ANN)
    assert strip[3] is None
    assert strip[0] is not None                          # …and only that slice


def test_the_strip_separates_steady_from_spiky_where_the_headline_cannot():
    """The reason the column exists. Two formulas: one that earned a little every stretch,
    one that was flat all year except a single blistering quarter. The headline Sharpe
    prefers the spike; the typical stretch does not."""
    n = 1200
    idx, rng = _index(n), np.random.default_rng(7)
    b = MW.calendar_buckets(idx, 12)
    noise = rng.normal(0.0, 0.01, n)

    steady = noise + 0.0016                              # a modest edge, every single day
    spiky = noise.copy()
    spiky[b == 5] += 0.028                               # one quarter does all the work

    assert _sharpe(spiky) > _sharpe(steady)              # the headline picks the spike…

    s_steady = MW.stability_strip(steady, b, ANN)
    s_spiky = MW.stability_strip(spiky, b, ANN)
    med = lambda s: float(np.median([v for v in s if v is not None]))   # noqa: E731
    assert med(s_steady) > med(s_spiky)                  # …the typical stretch does not

    neg = lambda s: sum(1 for v in s if v is not None and v < 0)        # noqa: E731
    assert neg(s_steady) < neg(s_spiky)                  # steady loses in fewer stretches
    assert max(v for v in s_spiky if v is not None) > 3 * med(s_steady)   # one huge block


def test_a_slice_is_a_noisy_sharpe_and_the_strip_shows_shape_not_precision():
    """The honest limit of the column. A twelfth of history is a short window, so an edge
    that is real still shows losing blocks — the strip is read for its SHAPE, and a single
    low block is not evidence of anything. This is why sorting uses the median."""
    n = 1200
    idx, rng = _index(n), np.random.default_rng(3)
    b = MW.calendar_buckets(idx, 12)
    real_edge = rng.normal(0.0, 0.01, n) + 0.0016        # a genuine, constant edge
    strip = MW.stability_strip(real_edge, b, ANN)
    assert _sharpe(real_edge) > 2.0                      # unmistakable over the whole span…
    assert any(v < 0 for v in strip if v is not None)    # …and still negative in some blocks


def test_strip_meta_marks_where_the_held_out_stretch_starts():
    n = 1200
    idx = _index(n)
    tmask = np.zeros(n, dtype=bool)
    tmask[900:] = True                                   # TEST is the last quarter of history
    ctx = {'market': {'index': idx}, 'tmask': tmask}
    meta = MW.strip_meta(ctx, 12)
    assert meta['n'] == 12
    assert meta['oos'] == 9                              # 900/1200 of 12 slices
    assert meta['start'] == '2020-01-01'
    assert sum(meta['bars']) == n


def test_strip_meta_survives_a_test_window_with_no_bars():
    idx = _index(100)
    ctx = {'market': {'index': idx}, 'tmask': np.zeros(100, dtype=bool)}
    assert MW.strip_meta(ctx, 12)['oos'] == 12           # nothing held out — no marker to place


# ---------------- how it reads ----------------
def _App():
    import alphanode_gui as G
    return G.App


def test_blocks_map_the_fixed_scale():
    App = _App()
    assert App._fmt_strip([0.0] * 12) == '▅' * 12        # mid height is Sharpe zero
    assert App._fmt_strip([9.0] * 12) == '█' * 12        # clipped at the top…
    assert App._fmt_strip([-9.0] * 12) == '▁' * 12       # …and at the bottom
    assert App._fmt_strip([None] * 12) == '·' * 12
    assert len(App._fmt_strip([0.1 * k for k in range(12)])) == 12


def test_the_scale_is_fixed_so_two_rows_can_be_compared():
    """A per-row scale would make every strip look the same shape — the column would say
    nothing about which formula is steadier, only about its own best and worst slice."""
    App = _App()
    small = App._fmt_strip([0.1] * 12)
    large = App._fmt_strip([1.9] * 12)
    assert small != large
    assert set(large) == {'█'} or max(large) > max(small)


def test_a_row_with_no_strip_says_so():
    App = _App()
    assert App._fmt_strip(None) == '—'
    assert App._fmt_strip([]) == '—'
    assert App._fmt_strip('nonsense') == '—'


def test_sorting_uses_the_typical_stretch_not_the_best_one():
    App = _App()
    spiky = {'strip': [-0.4] * 11 + [6.0]}
    steady = {'strip': [0.5] * 12}
    assert App._strip_typical(steady) > App._strip_typical(spiky)
    assert App._strip_typical({'strip': [None] * 12}) is None
    assert App._strip_typical({}) is None
    assert App._strip_typical({'strip': [1.0, None, 3.0]}) == pytest.approx(2.0)


def test_metrics_tuple_carries_the_strip_in_both_states():
    App = _App()
    assert App._fmt_metrics(None) == ('·',) * 10
    assert App._fmt_metrics('err') == ('—',) * 10


# ---------------- in the table ----------------
def test_the_column_is_shown_by_default_and_can_be_hidden(gui_app):
    app, _rec, _state = gui_app
    assert 'strip' in app.tree['displaycolumns']
    assert 'strip' in app._SORTABLE
    app._lb_toggle_col('strip')
    assert 'strip' not in app.tree['displaycolumns']
    app._lb_toggle_col('strip')
    assert 'strip' in app.tree['displaycolumns']
    assert not app._test_tk_errors


def test_the_cell_renders_from_the_metrics_cache(gui_app):
    app, _rec, _state = gui_app
    champ = {'formula': 'tanh(low)', 'base': 1.1, 'test': {'sharpe': 0.4}}
    app._metrics_cache[champ['formula']] = {
        'long': 10, 'short': 5, 'long_yr': 3.0, 'short_yr': 1.0, 'win': 0.5,
        'wup': None, 'wdown': None, 'act': 4.0, 'dd': -0.2, 'cagr': 0.1, 'sortino': 1.0,
        'tup': None, 'tdown': None, 'tflat': None,
        'strip': [0.0, 9.0, -9.0, None] * 3}
    app._treesig = None
    app._fill_tree([champ])
    item = app.tree.get_children()[0]
    assert app.tree.set(item, 'strip') == '▅█▁·' * 3
    assert not app._test_tk_errors


def test_a_saved_column_list_gains_the_new_column_once(gui_app, monkeypatch):
    """An existing install has a saved lb_cols that predates this column. It should appear
    for them — but exactly once, so hiding it afterwards sticks."""
    import json as _json

    import alphanode_gui as G
    app, _rec, _state = gui_app
    with open(G.SETTINGS, 'w', encoding='utf-8') as fh:
        _json.dump({'eula_accepted': '1.0.0', 'lb_cols': ['dd', 'cagr', 'id']}, fh)
    app.cfg = dict(G.DEFAULTS)
    app._load()
    assert 'strip' in app.cfg['lb_cols']                  # migrated in
    assert app.cfg['lb_cols_v2'] is True

    app.cfg['lb_cols'] = ['dd', 'cagr', 'id']             # the user hides it again
    with open(G.SETTINGS, 'w', encoding='utf-8') as fh:
        _json.dump(dict(app.cfg), fh)
    app.cfg = dict(G.DEFAULTS)
    app._load()
    assert 'strip' not in app.cfg['lb_cols']              # and it stays hidden


def test_the_column_is_wide_enough_for_twelve_blocks(gui_app):
    """Treeview columns are raw pixels while their text follows the DPI — the T-columns
    were clipped to 'T ↑ ·' once already. Twelve blocks must fit at this scale."""
    app, _rec, _state = gui_app
    need = app._tree_font.measure('▅' * MW.STRIP_N)
    assert app.tree.column('strip', 'width') >= need + 8
    assert app.tree.column('strip', 'minwidth') >= need + 8
