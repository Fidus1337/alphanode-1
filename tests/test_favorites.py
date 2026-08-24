"""Starred alphas: the module keeps them atomically outside the library lifecycle, the
leaderboard paints a ★ strip, the Favorites window opens rows like leaderboard rows."""
import json

import pytest

import favorites as favdb

CHAMP = {'formula': 'tanh(ts_mean:42(cs_rank(ema:159(volume))))',
         'base': 1.06, 'train': {'sharpe': 1.2}, 'val': {'sharpe': 1.06},
         'test': {'sharpe': 0.31}}


# ---------- the module ----------
def test_toggle_stars_then_unstars_and_persists(tmp_path):
    d = str(tmp_path)
    favs, added = favdb.toggle(d, CHAMP, '1h')
    assert added and len(favs) == 1
    assert favs[0]['formula'] == CHAMP['formula']
    assert favs[0]['tf'] == '1h' and favs[0]['added']    # frozen at star time
    assert favs[0]['test'] == {'sharpe': 0.31}
    assert favdb.ids(d) == {favdb.alpha_id(CHAMP['formula'])}
    favs2 = favdb.load(d)                                # a fresh read sees the same list
    assert favs2 == favs
    favs3, added3 = favdb.toggle(d, CHAMP, '1h')         # second toggle removes
    assert not added3 and favs3 == [] and favdb.load(d) == []


def test_corrupt_or_missing_file_reads_as_empty(tmp_path):
    d = str(tmp_path)
    assert favdb.load(d) == []                           # missing
    (tmp_path / favdb.FILE).write_text('{not json')
    assert favdb.load(d) == []                           # corrupt
    favs, added = favdb.toggle(d, CHAMP, '1d')           # and toggling still works after
    assert added and len(favdb.load(d)) == 1


def test_locked_docs_cannot_be_starred(tmp_path):
    with pytest.raises(ValueError):
        favdb.toggle(str(tmp_path), {'locked': True, 'id': 'abcdef123456', 'formula': ''}, '1h')


def test_remove_drops_by_id_only(tmp_path):
    d = str(tmp_path)
    other = dict(CHAMP, formula='ema:52(ema:93(ret))')
    favdb.toggle(d, CHAMP, '1h')
    favdb.toggle(d, other, '1h')
    left = favdb.remove(d, favdb.alpha_id(CHAMP['formula']))
    assert [f['formula'] for f in left] == [other['formula']]
    assert favdb.load(d) == left


def test_load_filters_formulaless_junk(tmp_path):
    (tmp_path / favdb.FILE).write_text(json.dumps(
        {'favorites': [{'formula': 'tanh(high)'}, {'no': 'formula'}, 'garbage', None]}))
    assert [f['formula'] for f in favdb.load(str(tmp_path))] == ['tanh(high)']


# ---------- the GUI ----------
def test_gui_star_toggle_paints_the_lb_and_persists(gui_app):
    app, rec, state = gui_app
    app._fav_toggle(dict(CHAMP))
    assert favdb.ids(str(state)) == {favdb.alpha_id(CHAMP['formula'])}
    app._treesig = None
    app._fill_tree([dict(CHAMP)])
    item = app.tree.get_children()[0]
    assert app.tree.set(item, 'fav') == '★'
    app._fav_toggle(dict(CHAMP))                         # unstar
    assert favdb.load(str(state)) == []
    app._treesig = None
    app._fill_tree([dict(CHAMP)])
    assert app.tree.set(app.tree.get_children()[0], 'fav') == ''
    assert not app._test_tk_errors


def test_gui_locked_rows_flash_instead_of_starring(gui_app):
    app, rec, state = gui_app
    app._fav_toggle({'locked': True, 'id': 'abcdef123456', 'formula': ''})
    assert favdb.load(str(state)) == []                  # nothing saved, no exception
    assert not app._test_tk_errors


def test_gui_favorites_window_lists_opens_and_unstars(gui_app, monkeypatch):
    app, rec, state = gui_app
    app._fav_toggle(dict(CHAMP))
    win = app._open_favorites()
    tv = win._tv
    kids = tv.get_children()
    assert len(kids) == 1
    assert tv.set(kids[0], 'formula').strip() == CHAMP['formula']
    assert tv.set(kids[0], 'id') == favdb.alpha_id(CHAMP['formula'])
    opened = []
    monkeypatch.setattr(app, '_open_plot', lambda c: opened.append(c))
    tv.selection_set(kids[0])
    tv.focus(kids[0])
    win._open()                                          # the double-click action
    assert opened and opened[0]['formula'] == CHAMP['formula']
    win._unstar()                                        # Remove ☆ / the Delete key
    assert tv.get_children() == () and favdb.load(str(state)) == []
    app._treesig = None                                  # and the leaderboard star is gone too
    app._fill_tree([dict(CHAMP)])
    assert app.tree.set(app.tree.get_children()[0], 'fav') == ''
    win.destroy()
    assert not app._test_tk_errors


def test_gui_star_strip_is_wired_first(gui_app):
    app, rec, state = gui_app
    assert app.tree['displaycolumns'][0] == 'fav'        # '#1' click = the star strip
    assert hasattr(app, 'btn_favs')
