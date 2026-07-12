# Building the AlphaNode desktop app

The app is packaged into a **self-contained binary** (PyInstaller): the Python interpreter and all
dependencies are inside — nothing needs to be installed on the target machine.

- **Linux** → `AlphaNode-x86_64.AppImage` (single file, double-click).
- **Windows** → `AlphaNode-Setup.exe` (Inno Setup installer: Start menu, shortcut, uninstaller)
  + `AlphaNode-windows-portable.zip` (a portable folder, no install).

## How it works (worth understanding)

The GUI spawns child processes (the search node, the Binance data fetcher, running a paper bundle). There is no ordinary
`python` alongside it in the built form, so the app launches **itself** with a role flag —
a single entry point [`alphanode/app_entry.py`](../alphanode/app_entry.py):

| Command | What it does |
|---|---|
| `AlphaNode` | GUI (default) |
| `AlphaNode --role node` | background search node |
| `AlphaNode --role fetch …` | Binance data fetcher |
| `AlphaNode --role runpy F …` | run a paper bundle's python file (the "▶ run" button) |
| `AlphaNode --role selfcheck` | bundle self-diagnostics (for CI/debugging) |

Paths are split into **read-only bundle resources** (the engine, `quantpylib/`, the default `data.pickle`,
`config.ini`) and a **writable user folder** — see [`alphanode/apppaths.py`](../alphanode/apppaths.py):

- Linux: `~/.local/share/AlphaNode/`
- Windows: `%APPDATA%\AlphaNode\`
- macOS: `~/Library/Application Support/AlphaNode/`

That's where `state/` (the alpha library, status), `exports/` (paper bundles), a fresh `data.pickle`,
and `gui_settings.json` are written. On first start `data.pickle` is seeded with a copy of the built-in snapshot, and after that
the fetcher overwrites exactly that file — just like in dev.

**Development mode is untouched:** with an ordinary `python alphanode/alphanode_gui.py` the paths and the launching
of child processes are exactly as before (alongside the code, via the real python).

## Build the Linux AppImage (locally)

```bash
bash packaging/build_linux.sh          # result: packaging/dist/AlphaNode-x86_64.AppImage
```
The script: installs PyInstaller into `.venv` (if absent) → draws the icon → PyInstaller (onedir) → assembles
`AppDir` (+ `.desktop`, icon) → pulls `appimagetool` → packs the `.AppImage`. Needs `curl` and
internet (for `appimagetool`). Variables: `PYTHON=…` (which python), `ARCH=x86_64`.

Quick check of the built binary without a window:
```bash
packaging/dist/AlphaNode/AlphaNode --role selfcheck     # prints a report + SELFCHECK OK, exit code 0
```

## Build a .deb for Ubuntu/Debian (delivering to people)

A native installer is the most convenient format for Ubuntu (double-click / `apt`, menu entry, **no
FUSE problems**, unlike AppImage on Ubuntu 24.04+):

```bash
bash packaging/build_deb.sh            # -> packaging/dist/alphanode_1.0.0_amd64.deb
bash packaging/build_deb.sh 1.2.0      # custom version
```
The script takes a ready PyInstaller build (or builds it), puts the app in `/opt/alphanode`,
adds a menu entry, an icon, and an `alphanode` command in the terminal (+ CLI: `alphanode --role cli top`).

**How the end user installs it** (no Python needed):
```bash
sudo apt install ./alphanode_1.0.0_amd64.deb     # or double-click in the file manager
# launch: applications menu -> AlphaNode, or `alphanode` in the terminal
sudo apt remove alphanode                         # remove
```

Inspect the package without installing: `dpkg-deb --info … .deb` and `dpkg-deb --contents … .deb`.

## Delivery (hosting)

- **GitHub Releases** — recommended: tag `vX.Y.Z` → CI builds and attaches to the downloads page
  the AppImage + Windows installer (see `build.yml`). The `.deb` can be attached there manually or
  added as a CI step.
- A direct file (Drive/website) — also works; for Ubuntu it's better to provide the `.deb`, for the rest of Linux — the AppImage.

## Build the Windows .exe + installer

Windows can't be built from this Linux machine — only on Windows or in CI. There's a ready workflow
[`.github/workflows/build.yml`](../.github/workflows/build.yml):

1. Push the project to a GitHub repository.
2. Actions → **build-desktop** → **Run workflow** (or push a tag `vX.Y.Z`).
3. When ready — the artifacts `alphanode-windows` (Setup.exe + portable.zip) and `alphanode-linux`
   (AppImage). With a tag, a **Release** with these files is additionally created.

Manually on a Windows machine:
```powershell
pip install -r packaging\requirements-build.txt
python packaging\make_icon.py
pyinstaller --noconfirm --clean --distpath packaging\dist --workpath packaging\build packaging\AlphaNode.spec
# installer (needs Inno Setup 6):
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\AlphaNode.iss
```

## Files

| File | Purpose |
|---|---|
| `AlphaNode.spec` | PyInstaller config (cross-platform) |
| `make_icon.py` | generates `alphanode.png`/`.ico` (Pillow, no external assets) |
| `build_linux.sh` | full AppImage build |
| `build_deb.sh` | native `.deb` build (Ubuntu/Debian) |
| `alphanode.desktop` | menu entry for the AppImage |
| `AlphaNode.iss` | Inno Setup installer (Windows) |
| `requirements-build.txt` | build dependencies |
| `../.github/workflows/build.yml` | CI: Windows + Linux + Release |

## Notes

- The data is **survivor** Binance coins (survivorship). The built-in `data.pickle` is a starting
  snapshot; the app has a button to download a fresh live universe.
- The AppImage is ~80 MB (numpy/pandas/matplotlib + Tk). `scipy/seaborn/statsmodels` are not part of
  the bundle (excluded in `.spec` — the runtime doesn't need them).
- If there's no `data.pickle` in the checkout, the build doesn't fail — the app simply asks to download the data.
