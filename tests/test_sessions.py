"""Sessions: the workspace-as-a-file layer. What these tests guard:

  * a snapshot must NEVER carry the subscription key (a session can travel to another
    machine or person), and restore must never overwrite THIS machine's key;
  * EVERY timeframe's library travels — including the suffixless daily files
    (library.jsonl / history.jsonl): the field bug where a 1d user's alphas were
    silently absent from every checkpoint;
  * restore is a full swap: files the archive does not carry must not survive and mix
    two workspaces — but files sessions do not own (device_id, data) are untouched;
  * ★ favorites are session-owned: a star points at a formula in a particular library, so
    it must not be inherited by a workspace mined on another basket, cut or timeframe;
  * a failed restore rolls the workspace back byte-for-byte; a failed snapshot leaves
    no file at all (no half-written archives under session names);
  * rotation reads the MANIFEST: named sessions are forever even if the user names one
    '..._auto'; identical back-to-back auto checkpoints are skipped;
  * loading the oldest auto snapshot works even when the before-load checkpoint
    rotates the pool (the target is extracted before the backup happens);
  * a malicious archive (absolute paths, ../, symlinks, FIFOs, oversized members) is
    rejected before any write.
"""
import io
import json
import os
import tarfile

import pytest

import sessions as S


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """An isolated workspace: state dir + settings file, sessions module pointed at them.
    Holds BOTH intraday (suffixed) and daily (suffixless) state files."""
    state = tmp_path / 'state'
    state.mkdir()
    settings = tmp_path / 'gui_settings.json'
    (state / 'library_1h.jsonl').write_text('{"f":1}\n{"f":2}\n{"f":3}\n')
    (state / 'history_1h.jsonl').write_text('{"r":1}\n')
    (state / 'library.jsonl').write_text('{"d":1}\n{"d":2}\n')   # the 1d files: no suffix
    (state / 'history.jsonl').write_text('{"r":1}\n')
    (state / 'forward.json').write_text(json.dumps({'entries': [
        {'id': 'a', 'archived': False, 'state': {'equity': 9950.0}},
        {'id': 'b', 'archived': True, 'state': {'equity': 111.0}}]}))
    (state / 'portfolio.json').write_text('{"top": 6}')
    (state / 'favorites.json').write_text(json.dumps({'favorites': [
        {'formula': 'tanh(low)', 'added': '2026-08-01'},
        {'formula': 'ema:12(close)', 'added': '2026-08-02'}]}))
    (state / 'device_id').write_text('MACHINE')          # identity: never in a session
    (state / 'library.jsonl.bak').write_text('old\n')    # stray backup: not ours
    settings.write_text(json.dumps({'tf': '1h', 'vault_license': 'SECRET-KEY-123',
                                    'target_vol': 0.25}))
    return str(state), str(settings)


def _grow(state, line='{"f":9}\n'):
    """Change the workspace so the next auto snapshot is not skipped as a duplicate."""
    open(os.path.join(state, 'library_1h.jsonl'), 'a').write(line)


def test_snapshot_manifest_and_secret_stripping(ws):
    state, settings = ws
    p = S.snapshot(name='my exp', note='n1', state_dir=state, settings_path=settings)
    assert os.path.exists(p)
    with tarfile.open(p) as tar:
        names = {m.name for m in tar.getmembers()}
        man = json.load(tar.extractfile('manifest.json'))
        cfg = json.load(tar.extractfile('settings.json'))
    assert 'state/device_id' not in names                # identity stays home
    assert 'state/library.jsonl.bak' not in names        # stray files stay home
    assert 'state/library_1h.jsonl' in names
    assert 'state/library.jsonl' in names                # THE field bug: 1d must travel
    assert 'state/history.jsonl' in names
    assert man['alphas'] == {'1h': 3, '1d': 2}
    assert man['forward'] == {'entries': 1, 'equity': 9950.0}   # archived entry not counted
    assert 'state/favorites.json' in names               # stars travel with their session
    assert man['favorites'] == 2
    assert man['name'] == 'my exp' and man['auto'] is False
    assert man['fp']                                     # content fingerprint present
    assert 'vault_license' not in cfg                    # THE invariant
    assert cfg['target_vol'] == 0.25


def test_restore_round_trip_swaps_the_whole_workspace(ws):
    state, settings = ws
    p = S.snapshot(name='base', state_dir=state, settings_path=settings)
    # workspace moves on: libraries grow, an extra timeframe appears, settings change
    open(os.path.join(state, 'library_1h.jsonl'), 'a').write('{"f":4}\n')
    open(os.path.join(state, 'library.jsonl'), 'a').write('{"d":3}\n')
    open(os.path.join(state, 'library_4h.jsonl'), 'w').write('{"x":1}\n')
    open(os.path.join(state, 'status.json'), 'w').write('{"round": 99}')   # transient
    json.dump({'tf': '4h', 'vault_license': 'SECRET-KEY-123'}, open(settings, 'w'))

    man = S.restore(p, state_dir=state, settings_path=settings)
    assert man['name'] == 'base'
    lines = open(os.path.join(state, 'library_1h.jsonl')).read().strip().splitlines()
    assert len(lines) == 3                               # back to the snapshot
    assert open(os.path.join(state, 'library.jsonl')).read().count('\n') == 2
    assert not os.path.exists(os.path.join(state, 'library_4h.jsonl'))  # no workspace mixing
    assert not os.path.exists(os.path.join(state, 'status.json'))       # stale status gone
    cfg = json.load(open(settings))
    assert cfg['tf'] == '1h'                             # session settings won...
    assert cfg['vault_license'] == 'SECRET-KEY-123'      # ...but the machine keeps its key
    assert open(os.path.join(state, 'device_id')).read() == 'MACHINE'
    # manual-save-only: restore() must NOT create any session on its own
    assert [m.get('name') for m in S.list_sessions(state)] == ['base']


def test_restore_never_installs_a_foreign_licence(ws):
    state, settings = ws
    p = S.snapshot(state_dir=state, settings_path=settings)
    # simulate a session file crafted WITH a licence inside (not one of ours)
    evil = p + '.evil.tar.gz'
    with tarfile.open(p) as src, tarfile.open(evil, 'w:gz') as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name == 'settings.json':
                d = json.loads(data)
                d['vault_license'] = 'STOLEN-KEY'
                data = json.dumps(d).encode()
                m.size = len(data)
            dst.addfile(m, io.BytesIO(data))
    json.dump({'tf': '1d'}, open(settings, 'w'))         # this machine has NO key
    S.restore(evil, state_dir=state, settings_path=settings)
    assert 'vault_license' not in json.load(open(settings))


def test_rotation_reads_the_manifest_not_the_filename(ws):
    state, settings = ws
    named = S.snapshot(name='precious', state_dir=state, settings_path=settings)
    trap = S.snapshot(name='exp_auto', state_dir=state, settings_path=settings)
    autos = []
    for i in range(8):
        _grow(state, f'{{"f":{i}}}\n')                   # distinct content each time
        autos.append(S.snapshot(auto=True, state_dir=state, settings_path=settings, keep=5))
    left = os.listdir(S.sessions_dir(state))
    assert os.path.basename(named) in left
    assert os.path.basename(trap) in left                # named '..._auto' is still named
    kinds = [m.get('auto') for m in S.list_sessions(state)]
    assert kinds.count(True) == 5                        # rotation trimmed real autos only
    assert os.path.basename(autos[-1]) in left           # the newest auto survived


def test_auto_checkpoints_skip_unchanged_workspace(ws):
    state, settings = ws
    p1 = S.snapshot(auto=True, skip_unchanged=True, state_dir=state, settings_path=settings)
    p2 = S.snapshot(auto=True, skip_unchanged=True, state_dir=state, settings_path=settings)
    assert p1 and p2 is None                             # a no-op stop makes no new file
    _grow(state)
    p3 = S.snapshot(auto=True, skip_unchanged=True, state_dir=state, settings_path=settings)
    assert p3
    assert len([n for n in os.listdir(S.sessions_dir(state)) if n.endswith('.tar.gz')]) == 2


def test_loading_the_oldest_auto_survives_the_before_load_rotation(ws):
    """The field bug: the before-load checkpoint used to rotate the pool BEFORE the
    target was read — loading the oldest of 10 autos deleted that very file."""
    state, settings = ws
    autos = []
    for i in range(10):
        _grow(state, f'{{"f":{i}}}\n')
        autos.append(S.snapshot(auto=True, state_dir=state, settings_path=settings))
    # backup=True is the opt-in path: it must checkpoint AFTER extracting the target,
    # so rotation can never eat the very file being loaded
    man = S.restore(autos[0], state_dir=state, settings_path=settings, backup=True)
    assert man['alphas']['1h'] == 4                      # base 3 + one _grow line
    lines = open(os.path.join(state, 'library_1h.jsonl')).read().strip().splitlines()
    assert len(lines) == 4


def test_failed_restore_rolls_the_workspace_back(ws, monkeypatch):
    state, settings = ws
    p = S.snapshot(name='base', state_dir=state, settings_path=settings)
    _grow(state)                                         # current differs from the archive
    before = {n: open(os.path.join(state, n), 'rb').read()
              for n in sorted(os.listdir(state)) if os.path.isfile(os.path.join(state, n))}

    real_replace = os.replace
    tripped = []
    def boom(src, dst):
        # fail ONCE while PLACING archive files into state/ (a locked/blocked target);
        # the rollback's own moves must then succeed
        if (not tripped and os.path.dirname(dst) == state
                and os.path.basename(dst) == 'portfolio.json'):
            tripped.append(1)
            raise OSError('disk went away')
        return real_replace(src, dst)
    monkeypatch.setattr(S.os, 'replace', boom)
    with pytest.raises(OSError, match='disk went away'):
        S.restore(p, state_dir=state, settings_path=settings, backup=False)
    monkeypatch.setattr(S.os, 'replace', real_replace)
    after = {n: open(os.path.join(state, n), 'rb').read()
             for n in sorted(os.listdir(state)) if os.path.isfile(os.path.join(state, n))}
    assert after == before                               # byte-for-byte rollback
    assert not [d for d in os.listdir(S.sessions_dir(state)) if d.startswith('.undo-')]


def test_failed_snapshot_leaves_no_file(ws, monkeypatch):
    state, settings = ws
    real = S._owned_state_files
    monkeypatch.setattr(S, '_owned_state_files',
                        lambda d: real(d) + [os.path.join(d, 'vanished.jsonl')])
    with pytest.raises(FileNotFoundError):
        S.snapshot(name='x', state_dir=state, settings_path=settings)
    left = os.listdir(S.sessions_dir(state))
    assert not [n for n in left if n.endswith('.tar.gz') or n.endswith('.partial')]


def test_malicious_archive_is_rejected(ws, tmp_path):
    state, settings = ws
    def evil_tar(*members):
        path = str(tmp_path / f'evil{len(os.listdir(tmp_path))}.tar.gz')
        with tarfile.open(path, 'w:gz') as tar:
            data = b'{}'
            info = tarfile.TarInfo('manifest.json'); info.size = 2
            tar.addfile(info, io.BytesIO(data))
            for m in members:
                tar.addfile(m, io.BytesIO(data) if m.isreg() else None)
        return path

    before = sorted(os.listdir(state))

    esc = tarfile.TarInfo('state/../../../evil.jsonl'); esc.size = 2
    with pytest.raises(ValueError, match='unsafe member'):
        S.restore(evil_tar(esc), state_dir=state, settings_path=settings)

    fifo = tarfile.TarInfo('state/library_1h.jsonl')     # right name, wrong beast
    fifo.type = tarfile.FIFOTYPE
    with pytest.raises(ValueError, match='unsafe member'):
        S.restore(evil_tar(fifo), state_dir=state, settings_path=settings)

    link = tarfile.TarInfo('settings.json')              # symlink to the outside world
    link.type = tarfile.SYMTYPE
    link.linkname = '/etc/passwd'
    with pytest.raises(ValueError, match='unsafe member'):
        S.restore(evil_tar(link), state_dir=state, settings_path=settings)

    assert sorted(os.listdir(state)) == before           # nothing was ever touched


def test_oversized_archive_is_rejected(ws, tmp_path, monkeypatch):
    state, settings = ws
    monkeypatch.setattr(S, 'MAX_TOTAL_BYTES', 1000)
    big = str(tmp_path / 'big.tar.gz')
    with tarfile.open(big, 'w:gz') as tar:
        info = tarfile.TarInfo('manifest.json'); info.size = 2
        tar.addfile(info, io.BytesIO(b'{}'))
        blob = b'0' * 4000                               # inflates past the (test) cap
        info = tarfile.TarInfo('state/library_1h.jsonl'); info.size = len(blob)
        tar.addfile(info, io.BytesIO(blob))
    before = sorted(os.listdir(state))
    with pytest.raises(ValueError, match='unreasonably large'):
        S.restore(big, state_dir=state, settings_path=settings)
    assert sorted(os.listdir(state)) == before


def test_rotation_ignores_unreadable_archives(ws):
    """A corrupt/foreign .tar.gz must neither occupy a keep-slot nor be deleted."""
    state, settings = ws
    junk = os.path.join(S.sessions_dir(state), '20990101-000000_junk_auto.tar.gz')
    open(junk, 'wb').write(b'not a tar at all')
    autos = []
    for i in range(4):
        _grow(state, f'{{"f":{i}}}\n')
        autos.append(S.snapshot(auto=True, state_dir=state, settings_path=settings, keep=3))
    left = os.listdir(S.sessions_dir(state))
    assert os.path.basename(junk) in left                # never deleted blindly
    assert sum(1 for m in S.list_sessions(state) if m.get('auto')) == 3   # real autos kept


def test_list_sessions_newest_first_with_sizes(ws):
    state, settings = ws
    S.snapshot(name='one', state_dir=state, settings_path=settings)
    import time
    time.sleep(1.1)                                      # filename stamp has 1s resolution
    S.snapshot(name='two', state_dir=state, settings_path=settings)
    ls = S.list_sessions(state)
    assert [m['name'] for m in ls] == ['two', 'one']
    assert all(m['size'] > 0 and m['path'].endswith('.tar.gz') for m in ls)


@pytest.mark.gui
def test_gui_manual_save_restore_and_rebuild(gui_app):
    """Manual-save-only world: a hand-saved session restores, nothing auto-saves along
    the way, and _sessions_rebuild repaints the leaderboard without the node."""
    app, rec, state = gui_app
    import alphanode_gui as G
    assert not hasattr(app, '_sessions_auto')            # the auto hook is gone for good
    lib = state / 'library_1h.jsonl'
    lib.write_text('{"f":"x","base":1.0}\n')

    saved = S.snapshot(name='by-hand', state_dir=str(state), settings_path=G.SETTINGS)
    lib.write_text('{"f":"x","base":1.0}\n{"f":"y","base":0.5}\n')   # workspace moves on...
    S.restore(saved, state_dir=str(state), settings_path=G.SETTINGS)
    assert lib.read_text().count('\n') == 1              # ...and comes back
    sdir = state / 'sessions'
    assert [n for n in os.listdir(sdir) if n.endswith('.tar.gz')] \
        == [os.path.basename(saved)]                     # no auto files appeared

    app._sessions_rebuild()                              # the window survives the swap
    assert not [c for c in rec.calls if c[0] == 'showerror']

    # the restored library must reach the LEADERBOARD with no node and no status.json:
    # the field bug where everything stayed blank until the next node run
    import time as _t
    deadline = _t.time() + 10
    while _t.time() < deadline and not app._lib_cache.get('computed'):
        app.root.update()
        _t.sleep(0.05)
    assert app._lib_cache.get('computed')
    app._refresh_leaderboard([])                         # what any poll tick now does
    app.root.update()
    assert len(app.tree.get_children()) == 1


def test_peek_reads_archive_without_touching_the_workspace(ws):
    state, settings = ws
    p = S.snapshot(name='look', state_dir=state, settings_path=settings)
    before = sorted(os.listdir(state))
    pk = S.peek(p)
    assert pk['manifest']['name'] == 'look'
    assert pk['manifest']['alphas'] == {'1h': 3, '1d': 2}
    assert 'vault_license' not in pk['settings']         # the key never even shows
    assert pk['settings']['target_vol'] == 0.25
    assert pk['portfolio'] == {'top': 6}
    assert sorted(os.listdir(state)) == before           # read-only, nothing extracted

    junk = os.path.join(S.sessions_dir(state), 'junk.tar.gz')
    open(junk, 'wb').write(b'not a tar')
    assert S.peek(junk) == {'manifest': None, 'settings': None, 'portfolio': None}


# ---- ★ favorites belong to the session that mined them -------------------------------

def test_stars_travel_with_the_session_and_do_not_leak_between_them(ws):
    """The reported problem: a star outlived the library it pointed into. Save session A
    with two stars, star something else, load A back — you get A's stars, not today's."""
    state, settings = ws
    a = S.snapshot(name='A', state_dir=state, settings_path=settings)
    json.dump({'favorites': [{'formula': 'rank(volume)'}]},
              open(os.path.join(state, 'favorites.json'), 'w'))
    b = S.snapshot(name='B', state_dir=state, settings_path=settings)

    S.restore(a, state_dir=state, settings_path=settings)
    got = json.load(open(os.path.join(state, 'favorites.json')))['favorites']
    assert [f['formula'] for f in got] == ['tanh(low)', 'ema:12(close)']

    S.restore(b, state_dir=state, settings_path=settings)
    got = json.load(open(os.path.join(state, 'favorites.json')))['favorites']
    assert [f['formula'] for f in got] == ['rank(volume)']    # B's star, not A's two


def test_a_session_saved_without_stars_restores_without_stars(ws):
    """A full swap, not a merge — the same rule every other owned file follows. Loading a
    starless workspace must not leave the previous one's ★ behind."""
    state, settings = ws
    os.remove(os.path.join(state, 'favorites.json'))
    p = S.snapshot(name='no-stars', state_dir=state, settings_path=settings)
    json.dump({'favorites': [{'formula': 'rank(volume)'}]},
              open(os.path.join(state, 'favorites.json'), 'w'))
    man = S.restore(p, state_dir=state, settings_path=settings)
    assert man['favorites'] == 0
    assert not os.path.exists(os.path.join(state, 'favorites.json'))


def test_starring_makes_the_workspace_look_changed(ws):
    """The fingerprint drives 'skip this auto checkpoint, nothing moved'. A star IS a
    change now, so a checkpoint taken after one must not be skipped as a duplicate."""
    state, settings = ws
    before = S.workspace_fingerprint(state, settings)
    doc = json.load(open(os.path.join(state, 'favorites.json')))
    doc['favorites'].append({'formula': 'rank(volume)'})
    json.dump(doc, open(os.path.join(state, 'favorites.json'), 'w'))
    assert S.workspace_fingerprint(state, settings) != before


def test_a_corrupt_favorites_file_is_counted_as_none(ws):
    """The manifest is written on every save — a hand-mangled star file must not stop one."""
    state, settings = ws
    open(os.path.join(state, 'favorites.json'), 'w').write('{ not json')
    p = S.snapshot(name='c', state_dir=state, settings_path=settings)
    with tarfile.open(p) as tar:
        man = json.load(tar.extractfile('manifest.json'))
    assert man['favorites'] == 0
    assert 'state/favorites.json' in {m.name for m in tarfile.open(p).getmembers()}


@pytest.mark.gui
def test_the_leaderboard_repaints_stars_after_a_session_load(gui_app):
    """_fav_ids is cached until something sets it to None. A restore swaps favorites.json
    under the GUI, so without the invalidation the table keeps painting the PREVIOUS
    workspace's ★ onto rows that belong to a different library."""
    app, _rec, state = gui_app
    import alphanode_gui as G
    import favorites as favdb
    lib = state / 'library_1h.jsonl'
    lib.write_text('{"formula":"tanh(low)","base":1.0}\n')
    favdb.toggle(str(state), {'formula': 'tanh(low)'}, '1h')
    assert favdb.ids(str(state)) == {favdb.alpha_id('tanh(low)')}
    starless = S.snapshot(name='starless', state_dir=str(state), settings_path=G.SETTINGS)

    app._fav_ids = {'deadbe'}                            # a star from the workspace we are
    S.restore(starless, state_dir=str(state), settings_path=G.SETTINGS)   # about to leave
    app._sessions_rebuild()
    live = app._fav_ids if app._fav_ids is not None else favdb.ids(str(state))
    assert 'deadbe' not in live                          # the stale star did not survive
    assert live == favdb.ids(str(state)) == {favdb.alpha_id('tanh(low)')}
