# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform build of AlphaNode (Linux AppImage / Windows .exe) with a single entry point
(alphanode/app_entry.py) that, based on --role, launches the GUI / node / data fetcher.

We build onedir (a folder) — for AppImage this is optimal (the AppImage itself is the compression layer).
On Windows CI, the .exe and installer are assembled from this same folder.
"""
import os
from PyInstaller.utils.hooks import collect_submodules

PROJ = os.path.dirname(SPECPATH)                     # packaging/ -> repository root
APP = os.path.join(PROJ, 'alphanode')

# The engine and its dependencies are imported dynamically (via sys.path) — list them explicitly, plus
# collect all quantpylib submodules (it's pulled in by the qt data fetcher and the portfolio engine).
hiddenimports = (
    ['fetch_data', 'apppaths', 'node', 'alphanode_gui', 'portfolio_build', 'cli',
     'signal_service', 'metrics_worker', 'pdf_report', 'pdf_worker', 'rescore_library',
     'forward_track',
     'config', 'evolution', 'evaluator', 'fastsim', 'genome', 'primitives',
     'report', 'evolved_strategy', 'experiments',
     'aiosonic', 'orjson', 'onecache', 'pytz',       # pytz: imported by quantpylib.wrappers.binance
     'customtkinter', 'darkdetect']                  # GUI toolkit (assets come from the contrib hook)
    + collect_submodules('quantpylib')
)

# Bundled default data snapshot (so the app works right away, before the first fetch).
# If the file is missing (e.g. a clean CI checkout) — don't fail the build; the app will prompt to download data.
datas = []
_data = os.path.join(PROJ, 'data.pickle')
if os.path.exists(_data):
    datas.append((_data, '.'))
else:
    print('AlphaNode.spec: data.pickle not found — building without a bundled snapshot')

# The vendor's PUBLIC vault key. Without it the node mines UNSEALED — the whole subscription
# model silently stops protecting anything — so a release build must not be allowed to omit it.
# Fetch before building:  curl -s https://api.<DOMAIN>/pub.txt > alphanode/vault_server_key.pub
_pub = os.path.join(APP, 'vault_server_key.pub')
if os.path.exists(_pub):
    datas.append((_pub, 'alphanode'))
elif os.environ.get('ALPHANODE_ALLOW_UNSEALED') == '1':
    print('AlphaNode.spec: no vault_server_key.pub — building an UNSEALED node (dev only)')
else:
    raise SystemExit(
        'AlphaNode.spec: alphanode/vault_server_key.pub is missing.\n'
        '  curl -s https://api.<YOUR-DOMAIN>/pub.txt > alphanode/vault_server_key.pub\n'
        '  (or set ALPHANODE_ALLOW_UNSEALED=1 for a deliberately unprotected dev build)')

# The ONE engine data file read at runtime: config.ini (the shipped defaults the ALPHANODE_*
# overrides layer over). load_config hard-fails on a missing path (config.py), and the GUI/node
# point ALPHANODE_CONFIG_INI here (apppaths.py), so it must ship — but nothing else from
# evolution/ does. Shipped explicitly now that the source Tree below is gone.
datas.append((os.path.join(PROJ, 'evolution', 'config.ini'), 'evolution'))

a = Analysis(
    [os.path.join(APP, 'app_entry.py')],
    pathex=[PROJ, APP, os.path.join(PROJ, 'evolution')],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['seaborn', 'statsmodels', 'IPython', 'pytest', 'notebook',
              'jupyterlab', 'tkinter.test'],
    noarchive=False,
)

# The engine ships as BYTECODE in the PYZ (see hiddenimports), never as source. It used to be
# dumped here as raw .py via Tree('evolution') — which handed the entire search algorithm to
# anyone who unzipped the AppImage. The mining path imports every engine module by name from the
# PYZ; the only file it reads off disk is config.ini, shipped explicitly above. app_entry's
# sys.path.insert for evolution/ is os.path.isdir-guarded, so the now-absent dir is a safe no-op.
#
# quantpylib stays as source: it is a third-party dependency (throttler + exchange wrappers), not
# our IP, and collect_submodules can miss its dynamic imports.
a.datas += Tree(os.path.join(PROJ, 'quantpylib'), prefix='quantpylib',
                excludes=['__pycache__', '*.pyc', '*.pyo'])

pyz = PYZ(a.pure)

_ico = os.path.join(SPECPATH, 'alphanode.ico')
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AlphaNode',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                                   # windowed application (no black console)
    disable_windowed_traceback=False,
    icon=(_ico if os.path.exists(_ico) else None),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='AlphaNode',
)
