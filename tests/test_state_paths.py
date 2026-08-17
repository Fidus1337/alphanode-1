"""Frozen-path regression tests: state and key paths must NEVER resolve relative to a
module's __file__.

FOUR shipped bugs came from exactly that. In a frozen build (AppImage squashfs, deb's /opt,
PyInstaller _MEIPASS) the module directory is a READ-ONLY bundle:
  * forward_track, portfolio_build and rescore_library each carried a "state next to the
    module" fallback — in the built app that meant makedirs() into the bundle (crash) or,
    worse, state silently written somewhere it could never be read back;
  * the GUI's vault-key resolver looked for vault_server_key.pub next to PROJ instead of
    inside the bundle (apppaths.RES_ROOT/alphanode/) — the miss returned '' and every
    shipped node silently mined IN THE OPEN, writing plaintext library_*.jsonl: the exact
    leak the vault exists to prevent.

These tests pin the fixed resolution order:
  ALPHANODE_STATE_DIR env  >  apppaths.state_dir()  >  (node only) <alphanode>/state,
and for the vault key:  ALPHANODE_VAULT_PUB env (verbatim)  >  RES_ROOT/alphanode/…  >  ''.
"""
import os

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)


def _import(modname):
    import importlib
    return importlib.import_module(modname)


# ---------------------------------------------------------------------------
# 1. forward_track / portfolio_build / rescore_library : _state_dir()
# ---------------------------------------------------------------------------

STATE_DIR_MODULES = ['forward_track', 'portfolio_build', 'rescore_library']


@pytest.mark.parametrize('modname', STATE_DIR_MODULES)
def test_state_dir_env_var_wins(modname, tmp_path, monkeypatch):
    """With ALPHANODE_STATE_DIR set, _state_dir() returns exactly that value —
    apppaths must not even be consulted (the GUI passes the env to its children)."""
    mod = _import(modname)
    d = tmp_path / 'env-state'
    monkeypatch.setenv('ALPHANODE_STATE_DIR', str(d))
    import apppaths

    def boom():                                     # env present -> apppaths must stay untouched
        raise AssertionError(f'{modname}._state_dir() consulted apppaths despite env')
    monkeypatch.setattr(apppaths, 'state_dir', boom)
    assert mod._state_dir() == str(d)


@pytest.mark.parametrize('modname', ['portfolio_build', 'rescore_library'])
def test_state_dir_falls_back_to_apppaths(modname, tmp_path, monkeypatch):
    """No env -> the ONLY fallback is apppaths.state_dir() (frozen-aware), never a path
    derived from the module's own __file__."""
    mod = _import(modname)
    monkeypatch.delenv('ALPHANODE_STATE_DIR', raising=False)
    import apppaths
    d = tmp_path / 'apppaths-state'
    monkeypatch.setattr(apppaths, 'state_dir', lambda: str(d))
    assert mod._state_dir() == str(d)


def test_forward_track_state_dir_falls_back_to_apppaths_and_creates(tmp_path, monkeypatch):
    """forward_track's variant additionally creates the dir (its callers write forward.json
    immediately) — the created dir must be the apppaths one, not <module>/state."""
    mod = _import('forward_track')
    monkeypatch.delenv('ALPHANODE_STATE_DIR', raising=False)
    import apppaths
    d = tmp_path / 'fwd-state'                      # deliberately does not exist yet
    monkeypatch.setattr(apppaths, 'state_dir', lambda: str(d))
    assert not d.exists()
    assert mod._state_dir() == str(d)
    assert d.is_dir(), 'forward_track._state_dir() must create the fallback dir'


def test_forward_track_state_dir_creates_env_dir_too(tmp_path, monkeypatch):
    """The env branch of forward_track also makedirs — pin that so a missing state dir on a
    cron box never turns into an ENOENT at the first forward.json write."""
    mod = _import('forward_track')
    d = tmp_path / 'env-state-created'
    monkeypatch.setenv('ALPHANODE_STATE_DIR', str(d))
    assert not d.exists()
    assert mod._state_dir() == str(d)
    assert d.is_dir()


# ---------------------------------------------------------------------------
# 2. node._default_state_dir()
# ---------------------------------------------------------------------------

def test_node_default_state_dir_uses_apppaths(tmp_path, monkeypatch):
    """node's default (no ALPHANODE_STATE_DIR) must come from apppaths — a direct
    `<exe> --role node` run (cron, docker) used to die on makedirs into the bundle."""
    node = _import('node')                          # import-time side effects sandboxed by conftest
    import apppaths
    d = tmp_path / 'node-state'
    monkeypatch.setattr(apppaths, 'state_dir', lambda: str(d))
    assert node._default_state_dir() == str(d)


def test_node_default_state_dir_last_resort_is_path_string_only(monkeypatch):
    """If apppaths itself blows up, the last resort is the dev-layout <alphanode>/state path
    STRING — _default_state_dir() itself must not create anything (creation is the caller's
    module-level makedirs, driven by whatever dir actually won)."""
    node = _import('node')
    import apppaths

    def boom():
        raise RuntimeError('simulated apppaths failure')
    monkeypatch.setattr(apppaths, 'state_dir', boom)
    expected = os.path.join(REPO_ROOT, 'alphanode', 'state')
    assert node._default_state_dir() == expected    # assert the path only — never mkdir it here


# ---------------------------------------------------------------------------
# 3. alphanode_gui._vault_pub_path()
# ---------------------------------------------------------------------------

def test_vault_pub_path_env_override_returned_verbatim(tmp_path, monkeypatch):
    """ALPHANODE_VAULT_PUB (self-host / dev override) is returned verbatim — no existence
    check, no rewriting: the operator said THIS key, the node must not second-guess it."""
    G = _import('alphanode_gui')
    p = str(tmp_path / 'does-not-even-exist.pub')
    monkeypatch.setenv('ALPHANODE_VAULT_PUB', p)
    assert G._vault_pub_path() == p


def test_vault_pub_path_dev_layout_finds_bundled_key(monkeypatch):
    """No env -> the key must resolve via apppaths.RES_ROOT/alphanode/ (in dev RES_ROOT is
    the repo root, so this is <repo>/alphanode/vault_server_key.pub — present in this
    checkout). The shipped bug resolved relative to PROJ, one level ABOVE the frozen
    bundle's _internal, returned '' and mined plaintext."""
    G = _import('alphanode_gui')
    monkeypatch.delenv('ALPHANODE_VAULT_PUB', raising=False)
    expected = os.path.join(REPO_ROOT, 'alphanode', 'vault_server_key.pub')
    got = G._vault_pub_path()
    assert got == expected
    assert os.path.isfile(got)


def test_vault_pub_path_no_key_anywhere_returns_empty(tmp_path, monkeypatch):
    """No env and no key under RES_ROOT -> '' (explicit 'unsealed' signal the callers and
    selfcheck test for), not a dangling path."""
    G = _import('alphanode_gui')
    monkeypatch.delenv('ALPHANODE_VAULT_PUB', raising=False)
    import apppaths
    empty = tmp_path / 'empty-res-root'
    empty.mkdir()
    monkeypatch.setattr(apppaths, 'RES_ROOT', str(empty))
    assert G._vault_pub_path() == ''


# ---------------------------------------------------------------------------
# 4. apppaths dev-mode sanity
# ---------------------------------------------------------------------------

def test_apppaths_dev_state_dir_is_alphanode_state():
    """In dev (not frozen) state lives at <repo>/alphanode/state — the 1:1 legacy layout the
    rest of the suite (and every doc) assumes."""
    import apppaths
    assert not apppaths.FROZEN                      # the test run itself is never a bundle
    d = apppaths.state_dir()
    assert d == os.path.join(REPO_ROOT, 'alphanode', 'state')
    assert d.endswith(os.path.join('alphanode', 'state'))


def test_apppaths_dev_res_root_is_repo_root():
    """In dev, read-only resources (evolution/, quantpylib/, data.pickle, the vault key dir)
    root at the repo itself."""
    import apppaths
    assert apppaths.RES_ROOT == REPO_ROOT
