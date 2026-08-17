"""Single source of truth for paths — accounting for PyInstaller (frozen) and plain runs from source.

Why: in the built application (AppImage / .exe) the code sits in a read-only bundle (`sys._MEIPASS`),
while writing (state, exports, a fresh data.pickle, settings) must go to a user folder. In development
mode (running `python alphanode/alphanode_gui.py`) behavior is 1:1 as before — next to the code.

  RES_ROOT  — read-only bundle resources: evolution/, quantpylib/, data.pickle, config.ini.
  USER_DIR  — where we write: ~/.local/share/AlphaNode  (Win: %APPDATA%\\AlphaNode; mac: ~/Library/...).
"""
import os
import sys
import shutil

FROZEN = bool(getattr(sys, 'frozen', False))

HERE = os.path.dirname(os.path.abspath(__file__))        # .../alphanode
PROJ = os.path.dirname(HERE)                             # repo root (in dev)


def _res_root():
    """Root of read-only resources. In the bundle — _MEIPASS; in dev — the repo root."""
    if FROZEN:
        return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    return PROJ


RES_ROOT = _res_root()


def _user_base():
    if sys.platform.startswith('win'):
        return os.environ.get('APPDATA') or os.path.expanduser('~')
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support')
    return os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')


def user_dir():
    """User (writable) application folder. In dev — alphanode/ itself (as before)."""
    if not FROZEN:
        return HERE
    d = os.path.join(_user_base(), 'AlphaNode')
    os.makedirs(d, exist_ok=True)
    return d


USER_DIR = user_dir()


def state_dir():
    d = os.path.join(USER_DIR, 'state') if FROZEN else os.path.join(HERE, 'state')
    os.makedirs(d, exist_ok=True)
    return d


def exports_dir():
    d = os.path.join(USER_DIR, 'exports') if FROZEN else os.path.join(PROJ, 'exports')
    os.makedirs(d, exist_ok=True)
    return d


def settings_file():
    return (os.path.join(USER_DIR, 'gui_settings.json') if FROZEN
            else os.path.join(HERE, 'gui_settings.json'))


def bundled_data():
    """Default data snapshot embedded in the bundle (in dev — the root data.pickle)."""
    return os.path.join(RES_ROOT, 'data.pickle')


def user_data_pickle():
    """Where the data fetcher writes the fresh data.pickle (writable). In dev — the root data.pickle."""
    return os.path.join(USER_DIR, 'data.pickle') if FROZEN else os.path.join(PROJ, 'data.pickle')


def data_path():
    """Stable writable path to the data. In the built form, on first start it is seeded with a copy
    of the embedded default — after that the data fetcher overwrites exactly this file (as in dev: the
    path is constant, the contents change), so fresh data is always picked up."""
    u = user_data_pickle()
    if FROZEN and not os.path.exists(u):
        b = bundled_data()
        if os.path.exists(b):
            try:
                shutil.copy2(b, u)
            except OSError:
                return b
    return u


def config_ini():
    return os.path.join(RES_ROOT, 'evolution', 'config.ini')


def license_file():
    """The bundled EULA (LICENSE.txt). In the bundle it sits at the resource root; in dev it is
    the repo-root LICENSE.txt. Returns the path whether or not it exists — callers check."""
    return os.path.join(RES_ROOT, 'LICENSE.txt') if FROZEN else os.path.join(PROJ, 'LICENSE.txt')


def engine_dir():
    """Directory with the formula-engine sources (imported at runtime via sys.path)."""
    return os.path.join(RES_ROOT, 'evolution')


def quant_dir():
    return os.path.join(RES_ROOT, 'quantpylib')


# In the built form the engine (config.py) reads these variables — set them for the GUI process
# itself too (it computes equity/signals in-process) so load_config() looks into the bundle/user folder.
if FROZEN:
    os.environ.setdefault('ALPHANODE_CONFIG_INI', config_ini())
    os.environ.setdefault('ALPHANODE_DATA', data_path())
