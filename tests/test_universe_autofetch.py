"""The universe simplifies to ONE explicit list (default: five majors) and Start grows a
data gate: missing snapshot / missing pair / stale snapshot -> auto-fetch of exactly the
configured basket, then Start resumes. The manual Download button and the top-N/min-history
knobs are gone; old settings migrate without changing anyone's basket."""
import json
import os
import pickle

import numpy as np
import pandas as pd

import alphanode_gui as G


def test_parse_universe_dedupes_uppercases_keeps_order():
    assert G._parse_universe(' sol , btcusdt,, SOL ,ethusdt ') == ['SOL', 'BTCUSDT', 'ETHUSDT']
    assert G._parse_universe('') == []
    assert G._parse_universe(None) == []
    assert G.DEFAULTS['universe_list'] == 'BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT'


def test_gui_collect_normalizes_and_empty_reverts_to_default(gui_app):
    app, rec, state = gui_app
    app.v_unilist.set('  ethusdt , btcusdt,, ETHUSDT ')
    assert app._collect()['universe_list'] == 'ETHUSDT,BTCUSDT'
    app.v_unilist.set('   ')
    assert app._collect()['universe_list'] == G.DEFAULTS['universe_list']


def test_gui_migration_all_pairs_becomes_the_snapshot_basket(gui_app):
    """conftest seeds the OLD shape (universe_all=True): the migration must fill the list
    with the pairs the user actually mined on and drop every retired key."""
    app, rec, state = gui_app
    assert 'universe_all' not in app.cfg
    assert 'fetch_n' not in app.cfg and 'fetch_years' not in app.cfg
    snap = app._snapshot_tickers()
    assert snap and app.cfg['universe_list'] == ','.join(snap)


def test_gui_migration_direct_reload_with_old_keys(gui_app):
    app, rec, state = gui_app
    json.dump({'universe_all': False, 'universe_list': 'btcusdt, ethusdt',
               'fetch_n': 40, 'fetch_years': 2, 'timeframe': '1h'},
              open(G.SETTINGS, 'w'))
    app.cfg = dict(G.DEFAULTS)
    app._load()
    assert app.cfg['universe_list'] == 'btcusdt, ethusdt'     # explicit list survives as-is
    assert 'universe_all' not in app.cfg and 'fetch_n' not in app.cfg
    json.dump({'universe_list': '  ,  ', 'timeframe': '1h'}, open(G.SETTINGS, 'w'))
    app.cfg = dict(G.DEFAULTS)
    app._load()
    assert app.cfg['universe_list'] == G.DEFAULTS['universe_list']   # garbage -> default five


def test_gui_panel_lost_the_knobs(gui_app):
    app, rec, state = gui_app
    for gone in ('v_uniall', 'v_fetchn', 'v_minyears', 'btn_fetch'):
        assert not hasattr(app, gone)
    assert hasattr(app, 'e_uni') and hasattr(app, 'v_unilist')
    assert app._universe_tickers() == G._parse_universe(app.cfg['universe_list'])


def _write_snap(path, symbols, end=None):
    end = end or pd.Timestamp.now(tz='UTC').floor('D')
    idx = pd.date_range(end=end, periods=60, freq='D', tz='UTC')
    dfs = [pd.DataFrame({c: np.linspace(1, 2, 60) for c in
                         ('open', 'high', 'low', 'close', 'volume')}, index=idx)
           for _ in symbols]
    with open(path, 'wb') as fh:
        pickle.dump((list(symbols), dfs), fh)


def test_gui_data_gap_matrix(gui_app, tmp_path, monkeypatch):
    app, rec, state = gui_app
    snap = tmp_path / 'data_1h.pickle'
    monkeypatch.setattr(app, '_data_file', lambda: str(snap))
    app.cfg['universe_list'] = 'BTCUSDT,ETHUSDT'

    need, why = app._data_gap()                          # no file at all
    assert need == ['BTCUSDT', 'ETHUSDT'] and 'no market data' in why

    _write_snap(snap, ['BTCUSDT'])                       # a configured pair is absent
    need, why = app._data_gap()
    assert need == ['BTCUSDT', 'ETHUSDT'] and 'ETHUSDT' in why

    _write_snap(snap, ['BTCUSDT', 'ETHUSDT', 'XXXUSDT'])   # superset is fine
    need, why = app._data_gap()
    assert need is None and why == ''

    stale_end = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=30)
    _write_snap(snap, ['BTCUSDT', 'ETHUSDT'], end=stale_end)
    need, why = app._data_gap()                          # all pairs there, but a month old
    assert need == ['BTCUSDT', 'ETHUSDT'] and 'days old' in why

    snap.write_bytes(b'garbage')                         # unreadable = absent
    need, why = app._data_gap()
    assert need and 'unreadable' in why


def test_gui_start_autofetches_then_resumes(gui_app, monkeypatch):
    app, rec, state = gui_app
    calls = []
    monkeypatch.setattr(app, '_data_gap', lambda: (['BTCUSDT', 'ETHUSDT'], 'test gap'))
    monkeypatch.setattr(app, '_auto_fetch',
                        lambda symbols, why, on_success=None: calls.append((symbols, why, on_success)))
    app.start()
    assert calls == [(['BTCUSDT', 'ETHUSDT'], 'test gap', app._start_after_fetch)]
    assert not (app.proc and app.proc.poll() is None)    # the node itself did NOT start


def test_node_ensure_data_headless_presence_check(tmp_path, monkeypatch):
    import sys, types
    import node

    ran = []
    stub = types.ModuleType('fetch_data')
    stub.DEFAULT_SYMBOLS = ('BTCUSDT', 'ETHUSDT')

    def _run(path, interval='1d', symbols=None, **kw):
        ran.append((interval, list(symbols)))
        _write_snap(path, symbols)
        return 0
    stub.run = _run
    monkeypatch.setitem(sys.modules, 'fetch_data', stub)

    snap = tmp_path / 'data.pickle'
    monkeypatch.setenv('ALPHANODE_DATA', str(snap))
    monkeypatch.setattr(node, 'UNIVERSE', 'BTCUSDT,ETHUSDT')

    node.ensure_data()                                   # no file -> fetch the basket
    assert ran == [(node.TF, ['BTCUSDT', 'ETHUSDT'])]

    node.ensure_data()                                   # complete -> untouched
    assert len(ran) == 1

    _write_snap(snap, ['BTCUSDT'])                       # a pair went missing -> refetch
    node.ensure_data()
    assert len(ran) == 2 and ran[-1][1] == ['BTCUSDT', 'ETHUSDT']

    monkeypatch.setattr(node, 'UNIVERSE', 'all')         # 'all' checks file presence only
    _write_snap(snap, ['BTCUSDT'])
    node.ensure_data()
    assert len(ran) == 2


def test_node_ensure_data_tops_up_without_shrinking(tmp_path, monkeypatch):
    """A shared snapshot with extra pairs must survive a top-up: the fetch takes the UNION,
    never just the configured basket (the old ensure_data never touched an existing file)."""
    import sys, types
    import node
    ran = []
    stub = types.ModuleType('fetch_data')
    stub.DEFAULT_SYMBOLS = ('BTCUSDT',)

    def _run(path, interval='1d', symbols=None, **kw):
        ran.append(list(symbols))
        _write_snap(path, symbols)
        return 0
    stub.run = _run
    monkeypatch.setitem(sys.modules, 'fetch_data', stub)
    snap = tmp_path / 'data.pickle'
    monkeypatch.setenv('ALPHANODE_DATA', str(snap))
    _write_snap(snap, ['LTCUSDT', 'DOGEUSDT', 'BTCUSDT'])
    monkeypatch.setattr(node, 'UNIVERSE', 'BTCUSDT,ETHUSDT,BTCUSDT')   # dup on purpose
    node.ensure_data()
    assert ran == [['LTCUSDT', 'DOGEUSDT', 'BTCUSDT', 'ETHUSDT']]      # union, deduped


def test_node_ensure_data_drops_unserved_pairs_loudly(tmp_path, monkeypatch, capsys):
    """fetch skips a pair Binance does not serve and still exits 0 — ensure_data must
    verify, shrink the universe, and keep mining (or die clearly when nothing is left)."""
    import sys, types
    import pytest
    import node
    stub = types.ModuleType('fetch_data')
    stub.DEFAULT_SYMBOLS = ('BTCUSDT',)

    def _run(path, interval='1d', symbols=None, **kw):
        _write_snap(path, [s for s in symbols if s != 'FOOUSDT'])      # FOO never delivered
        return 0
    stub.run = _run
    monkeypatch.setitem(sys.modules, 'fetch_data', stub)
    snap = tmp_path / 'data.pickle'
    monkeypatch.setenv('ALPHANODE_DATA', str(snap))
    monkeypatch.setattr(node, 'UNIVERSE', 'BTCUSDT,FOOUSDT')
    node.ensure_data()
    assert node.UNIVERSE == 'BTCUSDT'                    # shrunk, loudly
    assert os.environ['ALPHANODE_UNIVERSE'] == 'BTCUSDT'
    assert 'does not serve' in capsys.readouterr().out
    os.remove(snap)
    monkeypatch.setattr(node, 'UNIVERSE', 'FOOUSDT')     # nothing fetchable at all
    with pytest.raises(SystemExit):
        node.ensure_data()


def test_gui_migration_never_falls_back_to_the_1d_file(gui_app, tmp_path, monkeypatch):
    """universe_all=true with the ACTIVE tf's snapshot unreadable keeps the saved list —
    inventing a 50-pair intraday basket from the daily file was a monster first download."""
    app, rec, state = gui_app
    json.dump({'universe_all': True, 'universe_list': 'BTCUSDT,ETHUSDT', 'timeframe': '1h'},
              open(G.SETTINGS, 'w'))
    monkeypatch.setattr(app, '_data_file', lambda: str(tmp_path / 'nope.pickle'))
    app.cfg = dict(G.DEFAULTS)
    app._load()
    assert app.cfg['universe_list'] == 'BTCUSDT,ETHUSDT'


def test_gui_failed_fetch_retries_freely(gui_app, monkeypatch):
    """The guard must arm only after a SUCCESSFUL fetch that closed nothing. A failed one
    (offline, cancelled) never fires on_success — the next Start retries, no false error."""
    app, rec, state = gui_app
    calls = []
    monkeypatch.setattr(app, '_data_gap', lambda: (['BTCUSDT'], 'no market data yet'))
    monkeypatch.setattr(app, '_auto_fetch',
                        lambda symbols, why, on_success=None: calls.append(symbols))
    app.start()
    app.start()                                          # fetch 'failed' (no on_success ran)
    assert len(calls) == 2
    assert not any(c[0] == 'showerror' for c in rec.calls)


def test_gui_unserved_pairs_leave_the_universe_loudly(gui_app, tmp_path, monkeypatch):
    """The reconcile step: after a successful fetch the pairs Binance did not deliver drop
    out of the universe with a warning, and Start continues — no download-error loop over
    tickers the user may never have typed."""
    app, rec, state = gui_app
    snap = tmp_path / 'data_1h.pickle'
    _write_snap(snap, ['BTCUSDT', 'ETHUSDT'])            # what the fetch actually delivered
    monkeypatch.setattr(app, '_data_file', lambda: str(snap))
    app.cfg['universe_list'] = 'BTCUSDT,FOOUSDT,ETHUSDT'
    app.v_unilist.set(app.cfg['universe_list'])
    resumed = []
    monkeypatch.setattr(app, 'start', lambda: resumed.append(True))
    app._start_after_fetch()
    assert app.cfg['universe_list'] == 'BTCUSDT,ETHUSDT'
    assert app.v_unilist.get() == 'BTCUSDT,ETHUSDT'
    assert any(c[0] == 'showwarning' and 'FOOUSDT' in str(c[2]) for c in rec.calls)
    assert resumed == [True]
    assert json.load(open(G.SETTINGS))['universe_list'] == 'BTCUSDT,ETHUSDT'


def test_gui_backstop_when_reconcile_cannot_help(gui_app, tmp_path, monkeypatch):
    """Gap persists after a fetch, yet the snapshot is unreadable (nothing to reconcile
    against): the backstop arms, and the NEXT Start explains instead of looping."""
    app, rec, state = gui_app
    snap = tmp_path / 'data_1h.pickle'                   # never created
    monkeypatch.setattr(app, '_data_file', lambda: str(snap))
    app.cfg['universe_list'] = 'BTCUSDT'
    app.v_unilist.set('BTCUSDT')                         # start()'s _save() re-collects widgets
    calls = []
    monkeypatch.setattr(app, '_auto_fetch',
                        lambda symbols, why, on_success=None: calls.append(symbols))
    app._start_after_fetch()                             # arms the guard, falls into start()
    assert calls == [] or calls                          # start() ran: either fetch or error
    app.start()
    assert any(c[0] == 'showerror' and 'successful download' in str(c[2]) for c in rec.calls)



# ---- the chip editor (the universe as removable chips over the same v_unilist) ----

def test_chips_render_and_wrap(gui_app):
    app, _rec, _state = gui_app
    app.v_unilist.set('BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,ZECUSDT,DOGEUSDT,BNBUSDT,'
                      'LINKUSDT,1000PEPEUSDT,SUIUSDT')
    app.root.update_idletasks()
    wrap = int(app.UNI_WRAP * app.SCALE)
    rows = [w for w in app.uni_chips.winfo_children() if w.winfo_manager()]
    assert len(rows) > 1                                  # a long list wraps to more rows
    assert all(r.winfo_reqwidth() <= wrap + 4 for r in rows)   # none widens the pane
    texts = [c.winfo_children()[0].cget('text')
             for r in rows for c in r.winfo_children()]
    assert texts == app.v_unilist.get().split(',')        # chips ARE the var, in order


def test_chips_commit_parses_paste(gui_app):
    app, _rec, _state = gui_app
    app.v_unilist.set('BTCUSDT')
    app.e_uni.delete(0, 'end')
    app.e_uni.insert(0, ' adausdt, avaxusdt  btcusdt ')   # commas, spaces, a duplicate
    app._uni_commit()
    assert app.v_unilist.get() == 'BTCUSDT,ADAUSDT,AVAXUSDT'
    assert app.e_uni.get() == ''


def test_chips_whitespace_paste_splits(gui_app):
    """A newline/tab paste (spreadsheet column) splits into chips, never one giant token."""
    app, _rec, _state = gui_app
    app.v_unilist.set('')
    app.e_uni.delete(0, 'end')
    app.e_uni.insert(0, 'solusdt\nadausdt\tavaxusdt')
    app._uni_commit()
    assert app.v_unilist.get() == 'SOLUSDT,ADAUSDT,AVAXUSDT'


def test_chips_remove_edit_backspace(gui_app):
    app, _rec, _state = gui_app
    app.v_unilist.set('BTCUSDT,ETHUSDT,SOLUSDT')
    app._uni_remove('ETHUSDT')
    assert app.v_unilist.get() == 'BTCUSDT,SOLUSDT'
    app._uni_edit('BTCUSDT')                              # click a chip -> edit in the entry
    assert app.e_uni.get() == 'BTCUSDT'
    assert app.v_unilist.get() == 'SOLUSDT'
    app._uni_commit()                                     # and back
    assert app.v_unilist.get() == 'SOLUSDT,BTCUSDT'
    app.e_uni.delete(0, 'end')
    app._uni_backspace()                                  # empty box: the LAST CHIP drops down
    assert app.v_unilist.get() == 'SOLUSDT'               # into the box for editing —
    assert app.e_uni.get() == 'BTCUSDT'                   # never deleted outright
    app.e_uni.delete(0, 'end')
    app.e_uni.insert(0, 'X')
    assert app._uni_backspace() is None                   # text present: an ordinary backspace
    assert app.v_unilist.get() == 'SOLUSDT'
    app.e_uni.delete(0, 'end')


def test_chips_backspace_autorepeat_guard(gui_app):
    """X11 delivers a held key as a machine-gun of KeyPresses — without the 500ms guard a
    one-second hold emptied a whole hand-curated basket."""
    import types
    app, _rec, _state = gui_app
    app.v_unilist.set('BTCUSDT,ETHUSDT,SOLUSDT')
    app.e_uni.delete(0, 'end')
    app._uni_backspace(types.SimpleNamespace(time=1000))  # pulls SOLUSDT down
    assert app.e_uni.get() == 'SOLUSDT'
    app.e_uni.delete(0, 'end')                            # the held key erased it, then…
    app._uni_backspace(types.SimpleNamespace(time=1200))  # …auto-repeat 200ms later: guarded
    assert app.e_uni.get() == ''
    assert app.v_unilist.get() == 'BTCUSDT,ETHUSDT'
    app._uni_backspace(types.SimpleNamespace(time=1900))  # a deliberate later press acts
    assert app.e_uni.get() == 'ETHUSDT'


def test_a_typed_comma_is_just_a_character(gui_app):
    """The comma used to commit everything before it on KeyRelease. Two things went wrong
    with that: fast typists press the next letter before the comma's release (rollover), and
    — worse — the box emptied itself mid-word, which reads as the field eating your input.
    Now separators are resolved once, by Enter."""
    app, _rec, _state = gui_app
    app.v_unilist.set('BTCUSDT')
    app.e_uni.delete(0, 'end')
    app.e_uni.insert(0, 'eth,s')                          # mid-word: nothing has committed
    assert app.v_unilist.get() == 'BTCUSDT'
    assert app.e_uni.get() == 'eth,s'                     # what you typed is what you see
    app.e_uni.delete(0, 'end')
    app.e_uni.insert(0, 'xmrusdt, xlmusdt')
    app._uni_commit()                                     # Enter
    assert app.v_unilist.get() == 'BTCUSDT,XMRUSDT,XLMUSDT'
    assert app.e_uni.get() == ''


def test_chips_collect_commits_pending_entry_text(gui_app):
    """Type a pair and click START without Enter: CTk buttons never steal focus, so no
    FocusOut fires — _collect commits the box itself or the run drops the pair."""
    app, _rec, _state = gui_app
    app.v_unilist.set('BTCUSDT')
    app.e_uni.delete(0, 'end')
    app.e_uni.insert(0, 'dogeusdt')
    assert app._collect()['universe_list'] == 'BTCUSDT,DOGEUSDT'
    assert app.e_uni.get() == ''


def test_chips_reset_clears_pending_entry_text(gui_app):
    """Reset/session load must not leave typed text to ghost-commit on a later FocusOut."""
    app, _rec, _state = gui_app
    app.e_uni.delete(0, 'end')
    app.e_uni.insert(0, 'dogeusdt')
    app._apply_cfg_to_widgets()
    assert app.e_uni.get() == ''


def test_chips_save_shows_the_fallback_universe(gui_app):
    """All chips removed + Save/Start: _collect falls back to the default five — the panel
    shows what actually runs instead of keeping the 'no pairs' hint."""
    app, _rec, _state = gui_app
    app.v_unilist.set('')
    app._save()
    assert app.v_unilist.get() == G.DEFAULTS['universe_list']


def test_chips_giant_token_cannot_widen_the_pane(gui_app):
    """One unbroken pasted blob becomes a clamped chip, not a pane-widening one — and its
    ✕ stays visible so it can be removed."""
    app, _rec, _state = gui_app
    app.v_unilist.set('A' * 60)
    app.root.update_idletasks()
    wrap = int(app.UNI_WRAP * app.SCALE)
    rows = [w for w in app.uni_chips.winfo_children() if w.winfo_manager()]
    assert rows and all(r.winfo_reqwidth() <= wrap for r in rows)


def test_chips_ghost_double_click_is_dropped(gui_app):
    """A chip action re-renders and slides the neighbour under the cursor — the second
    press of a double-click must not act on it."""
    import types
    app, _rec, _state = gui_app
    app.v_unilist.set('BTCUSDT,ETHUSDT,SOLUSDT')
    app.root.update_idletasks()

    def ev(w, t):
        w.winfo_containing = lambda *_a: w                # pointer is over the widget
        return types.SimpleNamespace(widget=w, x_root=0, y_root=0, time=t)

    def first_label():
        row = app.uni_chips.winfo_children()[0]
        return row.winfo_children()[0].winfo_children()[0]

    app._uni_hit(ev(first_label(), 1000), app._uni_edit, 'BTCUSDT')
    assert app.e_uni.get() == 'BTCUSDT'                   # the real click acts
    app.root.update_idletasks()
    app._uni_hit(ev(first_label(), 1200), app._uni_edit, 'ETHUSDT')
    assert app.e_uni.get() == 'BTCUSDT'                   # the 200ms ghost is dropped
    assert 'ETHUSDT' in app.v_unilist.get()
    app._uni_hit(ev(first_label(), 1700), app._uni_edit, 'ETHUSDT')
    assert app.e_uni.get() == 'ETHUSDT'                   # a deliberate later click acts


def test_chips_empty_state_hint_and_collect_fallback(gui_app):
    app, _rec, _state = gui_app
    app.v_unilist.set('')
    app.root.update_idletasks()
    kids = app.uni_chips.winfo_children()
    assert kids and 'default five' in kids[0].cget('text')
    assert app._collect()['universe_list'] == G.DEFAULTS['universe_list']
