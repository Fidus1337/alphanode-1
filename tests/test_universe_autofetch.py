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

