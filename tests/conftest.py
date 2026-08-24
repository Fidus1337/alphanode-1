"""Shared fixtures. The ONE hard rule of this suite: no test may ever touch the real user
state — not ~/.local/share/AlphaNode, not alphanode/state, not alphanode/gui_settings.json
(it holds the live subscription key on user machines). Isolation happens at interpreter
level, BEFORE any application module is imported: several modules read ALPHANODE_* env at
import time, so pytest fixtures alone would be too late.
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# import layout mirrors how the app itself runs (GUI/node insert these at startup)
for _p in (os.path.join(ROOT, 'alphanode'), os.path.join(ROOT, 'evolution'), ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---- interpreter-level sandbox: set BEFORE any app import (module import order matters) ----
SANDBOX = tempfile.mkdtemp(prefix='alphanode-tests-')
os.environ['ALPHANODE_NO_SPLASH'] = '1'
os.environ['ALPHANODE_STATE_DIR'] = os.path.join(SANDBOX, 'state')
os.environ['XDG_DATA_HOME'] = os.path.join(SANDBOX, 'xdg')      # frozen-style user dir, if any
os.environ['ALPHANODE_CONFIG_INI'] = os.path.join(ROOT, 'evolution', 'config.ini')
os.environ['ALPHANODE_DATA'] = os.path.join(ROOT, 'data.pickle')
for _k in ('ALPHANODE_VAULT_PUB', 'ALPHANODE_VAULT_OPEN', 'ALPHANODE_VAULT_LICENSE',
           'ALPHANODE_VAULT_URL', 'ALPHANODE_TF'):
    os.environ.pop(_k, None)
os.makedirs(os.environ['ALPHANODE_STATE_DIR'], exist_ok=True)

import pytest  # noqa: E402


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A per-test state dir, exported for the test's own subprocesses too."""
    d = tmp_path / 'state'
    d.mkdir()
    monkeypatch.setenv('ALPHANODE_STATE_DIR', str(d))
    return d


@pytest.fixture(scope='session')
def keypair(tmp_path_factory):
    """A throwaway vault server keypair: (priv_path, pub_path, pub_bytes)."""
    import vault
    kdir = tmp_path_factory.mktemp('vaultkeys')
    priv_path = str(kdir / 'vault_key')
    vault.generate_keys(priv_path)
    pub_path = priv_path + '.pub'
    return priv_path, pub_path, vault.load_pub(pub_path)


@pytest.fixture()
def hub(tmp_path, keypair):
    """A live in-process AlphaHub over a temp SQLite + the throwaway keypair.
    Yields (TestClient, sqlite_conn) — the conn sees the same database as the app."""
    from fastapi.testclient import TestClient
    from alphahub import db as hubdb
    from alphahub.server import create_app
    priv_path, _pub_path, _pub = keypair
    db_path = str(tmp_path / 'hub.db')
    app = create_app(db_path, priv_path, webhook_secret='test-secret')
    with TestClient(app) as client:
        yield client, hubdb.connect(db_path)


class MessageboxRecorder:
    """Drop-in for tkinter.messagebox: records every dialog instead of blocking the run.
    askyesno answers False so nothing spawns follow-up subprocesses."""
    def __init__(self):
        self.calls = []

    def _rec(self, kind):
        def f(title, msg, **kw):
            self.calls.append((kind, title, msg))
            return False if kind == 'askyesno' else None
        return f

    def __getattr__(self, name):
        if name in ('showerror', 'showwarning', 'showinfo', 'askyesno', 'askokcancel'):
            return self._rec(name)
        raise AttributeError(name)


@pytest.fixture()
def gui_app(tmp_path, monkeypatch):
    """A real App on a hidden CTk root, with settings/state/dialogs fully sandboxed.
    Yields (app, recorder, state_dir). gui-marked tests need a DISPLAY."""
    if not (os.environ.get('DISPLAY') or sys.platform.startswith('win')):
        pytest.skip('no DISPLAY')
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setenv('ALPHANODE_STATE_DIR', str(state))
    import alphanode_gui as G
    monkeypatch.setattr(G, 'SETTINGS', str(tmp_path / 'settings.json'))
    monkeypatch.setattr(G, 'STATE_DIR', str(state))
    # apppaths.state_dir() ignores ALPHANODE_STATE_DIR in dev, so these module-level paths
    # were baked from the REAL alphanode/state at import — _poll would read the developer's
    # live status.json mid-test and paint real champions into a sandboxed leaderboard.
    for name in ('STATUS_FILE', 'SIGNALS_JSON', 'PORTFOLIO_JSON', 'PORTFOLIO_PNG'):
        monkeypatch.setattr(G, name, str(state / os.path.basename(getattr(G, name))))
    (tmp_path / 'settings.json').write_text(json.dumps(
        {'eula_accepted': '1.0.0', 'timeframe': '1h', 'universe_all': True}))
    rec = MessageboxRecorder()
    monkeypatch.setattr(G, 'messagebox', rec)
    import customtkinter as ctk
    root = ctk.CTk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *a: errors.append(a)
    app = G.App(root)
    for _ in range(30):                                  # drain deferred after() jobs
        root.update()
    app._test_tk_errors = errors
    yield app, rec, state
    try:
        root.destroy()
    except Exception:                                    # noqa: BLE001
        pass
