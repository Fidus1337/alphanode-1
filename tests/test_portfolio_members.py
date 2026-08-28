"""The built portfolio lists its members.

The card claims a combination beats its parts. Until now that claim was unfalsifiable on
screen: the summary named a count, the chart drew one line, and WHICH alphas went in lived
only inside portfolio.json. The members table puts each one next to its own held-out Sharpe,
so the diversification gain is either visible or it is not there.
"""
import hashlib
import pytest

pytestmark = pytest.mark.gui

LIB = [
    {'formula': 'tanh(low)', 'base': 1.23, 'test': {'sharpe': 0.41}},
    {'formula': 'rank(volume)', 'base': 0.80, 'test': {'sharpe': -0.10}},
    {'formula': 'wr(close)', 'base': 0.57, 'fit_metric': 'winrate', 'test': {'sharpe': 0.22}},
]


def _doc(**over):
    d = {'ok': True, 'n': 3, 'sel': 'base', 'tf': '1d', 'span': '2019..2026',
         'metrics': {'sharpe': 1.90, 'cagr': 0.30, 'dd': -0.20}, 'basket': {'sharpe': 0.20},
         'formulas_full': ['tanh(low)', 'rank(volume)', 'wr(close)'],
         'formulas': ['tanh(low)', 'rank(volume)', 'wr(close)'],
         'indiv_sharpe': [1.50, 0.30, 0.90], 'built_secs': 12.0,
         'equity': {'dates': [], 'combined': [], 'basket': []}}
    d.update(over)
    return d


@pytest.fixture()
def built(gui_app):
    app, rec, state = gui_app
    app._lib_cache['all'] = list(LIB)
    return app, rec


def _rows(app):
    return [[app.pf_tree.set(i, c) for c in app._PF_COLS] for i in app.pf_tree.get_children()]


def test_one_row_per_member_in_pick_order(built):
    app, _rec = built
    app._render_portfolio(_doc())
    rows = _rows(app)
    assert [r[0] for r in rows] == ['1', '2', '3']
    assert [r[5].strip() for r in rows] == ['tanh(low)', 'rank(volume)', 'wr(close)']
    assert int(app.pf_tree.cget('height')) == 3          # no scrollbar: every member is visible


def test_the_id_is_the_one_the_leaderboard_shows(built):
    """Same 6-char md5 tail, so a member can be found in the table above by eye."""
    app, _rec = built
    app._render_portfolio(_doc())
    want = hashlib.md5(b'tanh(low)').hexdigest()[:6]
    assert _rows(app)[0][1] == want


def test_solo_test_comes_from_the_build_and_the_rest_from_the_library(built):
    app, _rec = built
    app._render_portfolio(_doc())
    n, aid, solo, fit, test, _f = _rows(app)[0]
    assert solo == '+1.50'                               # indiv_sharpe: this alpha ALONE
    assert fit == '+1.23' and test == '+0.41'            # the leaderboard's own two numbers


def test_a_winrate_mined_member_reads_as_a_percentage(built):
    """The leaderboard prints a win-rate row's fitness as a share; the members table must
    not print 0.57 next to a column of Sharpes and let it pass for one."""
    app, _rec = built
    app._render_portfolio(_doc())
    assert _rows(app)[2][3] == '57%'


def test_a_member_the_library_no_longer_holds_still_shows(built):
    """A portfolio outlives the library it was built from — 'Clear all history' does not
    unbuild it. The member must stay listed, with honest dashes instead of numbers."""
    app, _rec = built
    app._lib_cache['all'] = [LIB[0]]
    app._render_portfolio(_doc())
    rows = _rows(app)
    assert len(rows) == 3
    assert rows[1][3] == '—' and rows[1][4] == '—'       # no fitness, no TEST OOS
    assert rows[1][2] == '+0.30'                         # …but SOLO came from the doc


def test_a_missing_solo_is_a_dash_not_a_zero(built):
    app, _rec = built
    app._render_portfolio(_doc(indiv_sharpe=[1.5, None]))
    rows = _rows(app)
    assert [r[2] for r in rows] == ['+1.50', '—', '—']   # None, then off the end of the list


def test_the_tint_marks_exactly_the_members_the_mix_beat(built):
    """The green row is the argument for combining at all: combined 1.90 clears 1.50, 0.30
    and 0.90 — but raise one member above the mix and its tint has to go."""
    app, _rec = built
    app._render_portfolio(_doc())
    assert all('pos' in app.pf_tree.item(i, 'tags') for i in app.pf_tree.get_children())
    app._render_portfolio(_doc(indiv_sharpe=[2.40, 0.30, 0.90]))
    tags = [app.pf_tree.item(i, 'tags') for i in app.pf_tree.get_children()]
    assert 'pos' not in tags[0]                          # 2.40 beat the 1.90 combination
    assert all('pos' in t for t in tags[1:])


def test_a_legacy_doc_without_full_formulas_still_lists(built):
    """Docs written before formulas_full carry only the 90-char truncations."""
    app, _rec = built
    d = _doc()
    d.pop('formulas_full')
    app._render_portfolio(d)
    assert len(_rows(app)) == 3


def test_clearing_the_panel_empties_the_table(built):
    app, _rec = built
    app._render_portfolio(_doc())
    app._reset_portfolio_ui()
    assert _rows(app) == []
    assert int(app.pf_tree.cget('height')) == 1


def test_double_click_charts_that_member(built, monkeypatch):
    app, _rec = built
    app._render_portfolio(_doc())
    seen = []
    monkeypatch.setattr(app, '_open_plot', lambda c: seen.append(c['formula']))
    app.pf_tree.selection_set(app.pf_tree.get_children()[1])
    app._pf_member_plot()
    assert seen == ['rank(volume)']                      # the row clicked, not the first one


def test_double_click_on_a_member_that_is_gone_says_so(built, monkeypatch):
    app, _rec = built
    app._render_portfolio(_doc())
    app._lib_cache['all'] = []
    said = []
    monkeypatch.setattr(app, '_open_plot', lambda c: said.append('PLOTTED'))
    monkeypatch.setattr(app, '_flash_lb', lambda m, **k: said.append(m))
    app.pf_tree.selection_set(app.pf_tree.get_children()[0])
    app._pf_member_plot()
    assert said and 'PLOTTED' not in said and 'no longer in the library' in said[0]
