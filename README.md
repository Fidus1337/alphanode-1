# AlphaNode

**An evolutionary alpha-search engine for crypto, with a desktop app.**

AlphaNode continuously *mines trading signals*. It runs genetic-programming search over a large space
of alpha-signal formulas, scores each one through a real portfolio simulator (`quantpylib`), and
accumulates the robust survivors into a library — the way a miner produces hashes, only here the
output is **alpha formulas** with an honest held-out track record.

You can drive it three ways from the same engine: a native **desktop GUI**, a **CLI** (server / ssh /
Docker), or the raw **search engine** in `evolution/`.

> ⚠️ Everything here is a *hypothetical backtest*, not investment advice. Any formula must be
> verified with a forward/paper test on **new** data before it means anything. See `disclaimer.txt`.

---

## What it actually does

The "DNA" of a strategy is a single formula that turns OHLCV into a per-instrument signal — e.g.
`ts_zscore(close,14)` (Bollinger) or `sign(sub(ts_mean(close,10),ts_mean(close,20)))` (MA-cross).
Everything else (inverse-vol sizing, forecast normalization, vol-targeting, position inertia, fees) is
the engine's fixed machinery. So the search space **strictly contains** the classic hand-written
strategies — and can beat them.

**Anti-overfitting is the whole point.** The data is split chronologically into **TRAIN → VAL → TEST**:

- Fitness = `min(train_Sharpe, val_Sharpe)` — a strategy is only as good as its *worst* segment, so
  the search is rewarded for robustness, not for curve-fitting.
- **TEST is held out** and never used for selection, ranking, or seeding. It is computed once and shown
  only as an honest out-of-sample estimate.
- Plus: complexity penalty, correlation dedup (a diverse Hall of Fame, not 12 clones), and a
  degeneracy filter (a signal must actually trade).

The search runs on `fastsim.py` — a numpy port of the simulator (~0.2 s/genome vs ~32 s on the real
pandas loop, ×110), cross-checked against the real engine (corr ≥ 0.99). Champions are then re-verified
on the real engine.

---

## Quickstart (run from source)

Needs Python 3.10+, a virtualenv with `numpy` / `pandas` / `matplotlib`, and `python3-tk` for the GUI
(the bundled `.venv` already has everything). A snapshot of 50 pairs ships in `data.pickle`, so it works
out of the box.

```bash
# Desktop GUI (recommended — all settings + live leaderboard + portfolio panel)
.venv/bin/python alphanode/alphanode_gui.py

# CLI — headless search, for server / ssh / Docker
.venv/bin/python alphanode/cli.py run --cpu 50 --pop 200 --gens 25
.venv/bin/python alphanode/cli.py top --sort test --min-test 1   # view the library
.venv/bin/python alphanode/cli.py status

# The raw search engine
cd evolution && ../.venv/bin/python run_evo.py --smoke
```

---

## How to use it

**GUI** (`alphanode/alphanode_gui.py`) — a CustomTkinter window, light or dark (the switch is in the
header; it follows the OS on first run). Left: the full set of search settings
(resources, universe, GA params, fitness, TRAIN/VAL/TEST boundaries). Right: live status, a progress
chart, and a scrollable **leaderboard** of **every mined alpha** (a **"families only"** switch collapses it to
the best per family). Click a column to sort — by honest fitness `min(train,val)` or by TEST OOS (⚠ the latter
is a cherry-pick on held-out data — for viewing only) — and see per-alpha long/short trade counts + daily win%,
computed lazily for the rows on screen so the full list stays smooth. **CSV** downloads the whole mined library.
Double-click a row for its equity curve with TRAIN|VAL|TEST zones. Bottom: a **PORTFOLIO** panel that combines the
top-N alphas through the real `Portfolio` engine and shows the diversified equity vs a buy & hold basket.

**CLI** (`alphanode/cli.py`) — the same knobs as flags. Subcommands: `run`, `fetch`, `top`, `status`,
`portfolio`, `signal`, `export`. State lives in `ALPHANODE_STATE_DIR` (default `alphanode/state/`), so
`top`/`status` see the library of a running node. Also runnable from the packaged binary:
`AlphaNode --role cli top`.

**Docker** — the same headless functionality (search + live web status, data fetch, portfolio builder,
signal API) in a container. See **[Run with Docker](#run-with-docker)** below.

**Update the data** — pull the top-N USDT perps by 24h turnover (public Binance endpoints, no keys):

```bash
.venv/bin/python fetch_data.py --top 150 --min-years 3     # atomically overwrites data.pickle
```

or the **⟳ Update data.pickle** button in the GUI. After changing the universe, clear the history and
restart the search (the old library was mined on different pairs).

**Paper trade a champion** — from the GUI's equity window, *📄 Paper Trade* assembles a self-contained
`exports/paper_<hash>/` bundle (strategy + a daily paper trader on live Binance data + engine copy) that
runs anywhere with no dependency on this repo.

**Serve live signals** — *📡 Serve signal (API)* (per alpha, or *📡 Serve* for the whole portfolio) starts
a local JSON service with the current target positions, recomputed on live Binance data every 15 min.
Any number of them run side by side: each takes the next free port from `8799` and gets a row in the
**SIGNAL API** card on the main screen — URL, log path, live `/health`, and *✕ Free port*. The services
are detached, so they keep serving after the GUI closes (it asks) and are re-adopted on the next start;
the registry lives in `state/signals.json`. Advisory signal only — no orders, no keys.

---

## Run with Docker

Everything the GUI does **except the desktop window** — the evolutionary search node with a live web
status page, data fetch, portfolio builder, and the signal API — packaged headless. The engine runs
identically; you drive it with the CLI and watch progress in a browser.

```bash
docker compose up -d --build node               # start the search; state persists in ./alphanode-data
#  → live leaderboard + progress at  http://localhost:8787

docker compose logs -f node                     # follow the search log
docker compose run --rm node top -n 20          # leaderboard in the terminal
docker compose run --rm node status             # node state
docker compose run --rm node fetch --top 150    # refresh Binance data (into the volume)
docker compose run --rm node portfolio --top 6  # combine top-6 alphas → state/portfolio.json
docker compose --profile signal up -d signal    # serve the top alpha's live signal on :8799
```

- **Status page** (`:8787`) is the browser view of the leaderboard + progress — the GUI's core, headless.
- **State** (mined library, node status, `data.pickle`) persists in `./alphanode-data`, mounted at `/data`.
- **Config** via `environment:` in `docker-compose.yml` or `-e ALPHANODE_*` (CPU share, universe, pop/gens,
  TRAIN/VAL/TEST boundaries, …) — the same keys as the GUI/CLI.
- **numba** is baked into the image (~4× faster node); it falls back to numpy if it can't load.

Plain Docker (no compose) works too:

```bash
docker build -t alphanode .
docker run -d -p 8787:8787 -v "$PWD/alphanode-data:/data" alphanode run --cpu 50
docker run --rm       -v "$PWD/alphanode-data:/data" alphanode top -n 20
```

> The daemon must be reachable — add your user to the `docker` group
> (`sudo usermod -aG docker $USER`, then re-login) or prefix the commands with `sudo`.

---

## Configuration

Everything the engine understands is configurable, in three layers (later overrides earlier):

1. `evolution/config.ini` — the default baseline (target vol, fees, pop/gens, genome limits, GA & fitness
   params, TRAIN/VAL/TEST boundaries, the universe).
2. `ALPHANODE_*` environment variables — the same keys (used by CLI / Docker / the env file).
3. GUI settings — saved to `gui_settings.json`, passed to the node as `ALPHANODE_*` on start.

`ALPHANODE_CPU_PERCENT` (5–95) → parallel workers = `% × cores`; the node runs at background priority.

---

## Project layout

| Path | What it is |
|------|------------|
| `alphanode/` | The desktop app: GUI, CLI, background node, portfolio builder, paper-trade export. See `alphanode/README.md`. |
| `evolution/` | The genetic-programming search engine (primitives, genome, evaluator, GA loop, fastsim). See `evolution/README.md`. |
| `quantpylib/` | Vendored simulation engine — the `Alpha` / `Portfolio` objects that turn a signal into a NET-of-fees equity curve. |
| `fetch_data.py` | Binance OHLCV fetcher → `data.pickle`. |
| `data.pickle` | Bundled OHLCV snapshot (50 pairs) so the app runs out of the box. |
| `strategies.py`, `paper_trade.py`, `run_paper.sh` | Legacy hand-coded strategies (Bollinger/MA/Donchian/RSI) + a standalone daily paper trader. |
| `research/` | Jupyter experiments (e.g. a direction-classifier study). |
| `packaging/` | Build tooling: AppImage (`build_linux.sh`), `.deb` (`build_deb.sh`), the PyInstaller spec, Windows installer, icons. |
| `Dockerfile`, `docker-compose.yml`, `docker/` | Headless container — search node + web status, fetch, portfolio, signal API. See **[Run with Docker](#run-with-docker)**. |
| `.github/` | CI — the Windows build workflow. |

Generated at runtime and git-ignored: `alphanode/state/` (the mined library + node status),
`exports/`, logs, and PyInstaller build outputs.

---

## Building a desktop app

Packaged as a self-contained binary (Python + deps inside — nothing to install on the target machine):

```bash
bash packaging/build_linux.sh     # → AlphaNode-x86_64.AppImage (any Linux, single file)
bash packaging/build_deb.sh       # → alphanode_*.deb (Ubuntu/Debian, menu entry)
# Windows → AlphaNode-Setup.exe + portable zip, via .github/workflows (run by tag vX.Y.Z)
```

Internals (the `--role node/fetch/cli/portfolio/signal/metrics/runpy` dispatch, the user-data folder, manual builds)
are documented in `packaging/README.md`.

---

## More detail

- `alphanode/README.md` — the node, GUI, CLI, Docker, and paper trading in depth.
- `evolution/README.md` — the search engine, the DSL grammar, overfitting discipline, and reuse
  (re-scoring the library / warm-start).
- `packaging/README.md` — building and shipping the desktop app.
