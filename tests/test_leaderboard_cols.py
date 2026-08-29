"""The leaderboard's column ORDER — specifically, where ID sits.

ID is the row's name: the six hex characters you carry into the forward track, the
portfolio and every CSV. Buried between CAGR and sortino it read as one more statistic;
it belongs next to '#', which is the other column that identifies a row rather than
scoring it. These tests pin that position and the two ways it can move — the user hiding
the column, and the header menu that hides it.
"""
import pytest

pytestmark = pytest.mark.gui


def test_id_sits_second_by_default(gui_app):
    app, _rec, _state = gui_app
    disp = list(app.tree['displaycolumns'])
    assert disp[:4] == ['fav', 'rank', 'id', 'fit']      # ★ · # · ID · fitness
    assert disp.index('id') < disp.index('test')         # ahead of the whole analysis block
    assert disp[-1] == 'formula'


def test_hiding_id_leaves_the_rest_in_order(gui_app):
    app, _rec, _state = gui_app
    before = [c for c in app.tree['displaycolumns'] if c != 'id']
    app._lb_toggle_col('id')
    assert 'id' not in app.tree['displaycolumns']
    assert list(app.tree['displaycolumns']) == before     # no gap, no reshuffle
    assert 'id' not in app.cfg['lb_cols']                # and the choice is saved
    app._lb_toggle_col('id')                             # back on -> back to slot 2
    assert list(app.tree['displaycolumns'])[:3] == ['fav', 'rank', 'id']


def test_id_leads_the_header_menu(gui_app):
    app, _rec, _state = gui_app
    assert app._LB_OPT_ORDER[0] == 'id'                  # menu order mirrors screen order
    assert app._cols_menu.entrycget(0, 'label') == app._HEAD['id']


def test_the_id_cell_still_carries_the_value(gui_app):
    """Order is displaycolumns only — values stay keyed by name, so nothing shifts."""
    app, _rec, _state = gui_app
    champ = {'formula': 'tanh(low)', 'base': 1.23, 'test': {'sharpe': 0.5}}
    app._treesig = None
    app._fill_tree([champ])
    item = app.tree.get_children()[0]
    aid = app.tree.set(item, 'id')
    assert len(aid) == 6 and all(c in '0123456789abcdef' for c in aid)
    assert app.tree.set(item, 'fit') == '+1.23'          # neighbours unchanged
    assert not app._test_tk_errors
