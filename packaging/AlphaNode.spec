# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform build of AlphaNode (Linux AppImage / Windows .exe) with a single entry point
(alphanode/app_entry.py) that, based on --role, launches the GUI / node / data fetcher / paper bundle.

We build onedir (a folder) — for AppImage this is optimal (the AppImage itself is the compression layer).
On Windows CI, the .exe and installer are assembled from this same folder.
"""
import os
from PyInstaller.utils.hooks import collect_submodules

PROJ = os.path.dirname(SPECPATH)                     # packaging/ -> repository root
APP = os.path.join(PROJ, 'alphanode')

# The engine and its dependencies are imported dynamically (via sys.path) — list them explicitly, plus
# collect all quantpylib submodules (it's pulled in by the qt data fetcher and the paper bundle).
hiddenimports = (
    ['fetch_data', 'apppaths', 'node', 'alphanode_gui', 'paper_export', 'portfolio_build', 'cli',
     'signal_service',
     'config', 'evolution', 'evaluator', 'fastsim', 'genome', 'primitives',
     'report', 'evolved_strategy', 'experiments',
     'aiosonic', 'orjson', 'onecache']
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

# We ship the raw engine and quantpylib sources as DATA: (1) the engine imports them at runtime,
# (2) the paper-bundle generator (paper_export) copies them — the files must be present on disk.
a.datas += Tree(os.path.join(PROJ, 'evolution'), prefix='evolution',
                excludes=['__pycache__', '*.pyc', '*.pyo'])
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
