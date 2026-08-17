"""Forward-track enrollment: the on-disk track and the GUI click chain.

Guards the invariants behind a real shipped bug: the "Forward track ➕" button silently did
NOTHING in the frozen build — forward_track resolved its state dir next to the module (the
read-only AppImage/deb bundle), save_track's open() raised OSError inside the Tk callback,
and Tk swallowed the traceback, so no dialog, no forward.json, no error. These tests would
have caught it three ways: (A) load/save must live under ALPHANODE_STATE_DIR and survive a
missing/corrupt file, and (B) a real App's _fwd_enroll must actually produce forward.json in
the state dir, freeze the ACTIVE timeframe's universe (another past bug: 1h alphas frozen on
the 1d basket), confirm with a dialog, and leave the Tk error hook empty.
"""
import hashlib
import json
import os
import pickle

import pytest

import forward_track as ft

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORMULA = 'ema:10(ema:16(logret))'


# ---------------------------------------------------------------- Part A: the library itself

def test_new_entry_field_shape():
    e = ft.new_entry('alpha_t01', 'alpha', [FORMULA], ['BTCUSDT', 'ETHUSDT'],
                     0.25, 0.001, '2019-09-05', tf='1h')
    assert set(e) == {'id', 'name', 'kind', 'tf', 'formulas', 'tickers', 'vol', 'exec',
                      'engine_start', 'start_capital', 'enrolled', 'archived', 'state',
                      'history'}
    assert e['name'] == 'alpha_t01' and e['kind'] == 'alpha' and e['tf'] == '1h'
    assert e['formulas'] == [FORMULA]
    assert e['tickers'] == ['BTCUSDT', 'ETHUSDT']
    assert isinstance(e['vol'], float) and e['vol'] == 0.25
    assert isinstance(e['exec'], float) and e['exec'] == 0.001
    assert e['engine_start'] == '2019-09-05'          # trimmed to the date part
    assert e['start_capital'] == ft.START_CAPITAL
    assert e['archived'] is False
    assert e['history'] == []
    assert e['state'] == {'equity': ft.START_CAPITAL, 'positions': {}, 'prices': {},
                          'last_run': None}
    # enrolled is today's UTC date in ISO form
    assert len(e['enrolled']) == 10 and e['enrolled'][4] == '-' and e['enrolled'][7] == '-'


def test_new_entry_signature_matches_frozen_strategy():
    tickers = ['ETHUSDT', 'BTCUSDT']
    e = ft.new_entry('alpha_t01', 'alpha', [FORMULA], tickers, 0.25, 0.001,
                     '2019-09-05', tf='1h')
    sig = hashlib.md5((FORMULA + '#' + ','.join(sorted(tickers)) + '#1h')
                      .encode()).hexdigest()[:6]
    assert e['id'] == f'alpha_t01_{sig}'
    # ticker ORDER must not change the identity of the frozen strategy…
    e2 = ft.new_entry('alpha_t01', 'alpha', [FORMULA], list(reversed(tickers)),
                      0.25, 0.001, '2019-09-05', tf='1h')
    assert e2['id'] == e['id']
    # …but the bar size must: the same formula on 1d bars is a different strategy
    e3 = ft.new_entry('alpha_t01', 'alpha', [FORMULA], tickers, 0.25, 0.001,
                      '2019-09-05', tf='1d')
    assert e3['id'] != e['id']


def test_find_duplicate_ignores_ticker_order():
    e = ft.new_entry('a', 'alpha', [FORMULA], ['BTCUSDT', 'ETHUSDT'], 0.25, 0.001,
                     '2019-09-05', tf='1h')
    track = {'entries': [e]}
    dup = ft.find_duplicate(track, [FORMULA], ['ETHUSDT', 'BTCUSDT'], tf='1h')
    assert dup is e


def test_find_duplicate_distinguishes_timeframes():
    e = ft.new_entry('a', 'alpha', [FORMULA], ['BTCUSDT', 'ETHUSDT'], 0.25, 0.001,
                     '2019-09-05', tf='1h')
    track = {'entries': [e]}
    assert ft.find_duplicate(track, [FORMULA], ['BTCUSDT', 'ETHUSDT'], tf='1d') is None


def test_find_duplicate_skips_archived_entries():
    e = ft.new_entry('a', 'alpha', [FORMULA], ['BTCUSDT'], 0.25, 0.001,
                     '2019-09-05', tf='1h')
    e['archived'] = True
    assert ft.find_duplicate({'entries': [e]}, [FORMULA], ['BTCUSDT'], tf='1h') is None


def test_load_track_missing_file_yields_empty_track(sandbox):
    assert ft.track_file() == str(sandbox / 'forward.json')
    assert not os.path.exists(ft.track_file())
    assert ft.load_track() == {'entries': []}


def test_load_track_corrupt_file_yields_empty_track(sandbox):
    (sandbox / 'forward.json').write_text('{"entries": [oops — not json')
    assert ft.load_track() == {'entries': []}


def test_save_track_load_track_roundtrip_in_state_dir(sandbox):
    e = ft.new_entry('alpha_t01', 'alpha', [FORMULA], ['BTCUSDT', 'ETHUSDT'],
                     0.25, 0.001, '2019-09-05', tf='1h')
    ft.save_track({'entries': [e]})
    # the write MUST land in ALPHANODE_STATE_DIR (the shipped bug wrote into the bundle)
    assert (sandbox / 'forward.json').exists()
    assert not (sandbox / 'forward.json.tmp').exists()    # atomic replace left no temp file
    got = ft.load_track()
    assert got == {'entries': [e]}


# ------------------------------------------------- Part B: the real GUI click chain (needs X)

def _expected_1h_tickers():
    with open(os.path.join(ROOT, 'data_1h.pickle'), 'rb') as f:
        return list(pickle.load(f)[0])


def _enroll_once(app):
    app._fwd_enroll([FORMULA], 'alpha_t01', 'alpha')


@pytest.mark.gui
def test_gui_enroll_writes_forward_json_and_confirms(gui_app):
    app, rec, state = gui_app
    _enroll_once(app)
    fj = state / 'forward.json'
    assert fj.exists(), 'the ➕ click chain never wrote forward.json — the shipped-build bug'
    doc = json.loads(fj.read_text())
    assert len(doc['entries']) == 1
    e = doc['entries'][0]
    assert e['tf'] == '1h'                                # frozen with the ACTIVE timeframe
    assert e['tickers'] == _expected_1h_tickers()         # the 1h basket, not the 1d fallback
    assert e['formulas'] == [FORMULA]
    assert not e.get('archived')
    kinds = [c[0] for c in rec.calls]
    assert 'showerror' not in kinds and 'showwarning' not in kinds
    ask = [c for c in rec.calls if c[0] == 'askyesno']
    assert len(ask) == 1 and 'enrolled' in ask[0][2]


@pytest.mark.gui
def test_gui_second_identical_enroll_is_rejected_as_duplicate(gui_app):
    app, rec, state = gui_app
    _enroll_once(app)
    rec.calls.clear()
    _enroll_once(app)
    doc = json.loads((state / 'forward.json').read_text())
    assert len(doc['entries']) == 1, 'duplicate enroll appended a second entry'
    infos = [c for c in rec.calls if c[0] == 'showinfo']
    assert len(infos) == 1 and 'Already enrolled' in infos[0][2]
    assert not any(c[0] == 'askyesno' for c in rec.calls)  # no second "enrolled" confirmation


@pytest.mark.gui
def test_gui_fwd_refresh_is_clean_and_tk_swallows_no_errors(gui_app):
    app, rec, state = gui_app
    _enroll_once(app)
    app._fwd_refresh()                                    # must not raise with a live entry
    for _ in range(10):                                   # flush any deferred after() jobs
        app.root.update()
    assert app._test_tk_errors == [], (
        'a Tk callback raised and the exception was swallowed — exactly how the shipped '
        f'bug hid: {app._test_tk_errors}')


@pytest.mark.gui
def test_gui_universe_tickers_honors_explicit_list(gui_app):
    app, rec, state = gui_app
    assert app._universe_tickers() == _expected_1h_tickers()   # universe_all=true baseline
    app.cfg['universe_all'] = False
    app.cfg['universe_list'] = ' btcusdt, ETHUSDT ,solusdt '
    assert app._universe_tickers() == ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
    app.cfg['universe_list'] = '  ,  '
    assert app._universe_tickers() is None                     # empty list is "no universe"
