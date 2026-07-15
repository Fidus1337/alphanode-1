# AlphaNode

A background strategy-search node. Start it once, and it **continuously mines alphas**:
it runs evolutionary search in rounds, accumulates the champions it finds into a library, and eats
exactly as many resources as you allow. Minimal interface — a live status page.

Like a miner, only instead of hashes it produces robust alpha formulas (`min(train,val)` fitness,
held-out TEST). The core is the `../evolution` engine.

## How it searches (continuously, getting better)

The node runs **forever** (`MAX_ROUNDS=0`) and **progressively converges on the best**, rather than
churning through random guesses:
- most rounds — a **warm-start from its own library** (seed the population with the best alphas
  found so far → fine-tune, refine);
- every `EXPLORE_EVERY`-th round (4th by default) — **pure exploration** from scratch, to avoid
  getting stuck in a local optimum;
- the best-of-all-time accumulates in `library.jsonl` and is never lost.

**We keep TEST held-out (anti-overfitting).** Champion selection, the leaderboard, and warm-start seeds
are driven **strictly by the fitness `base = min(train,val)-Sharpe`**. TEST plays NO part in selection: we
compute and show it only as an honest held-out one-shot (`TEST OOS` in the leaderboard, an annotation on the chart).
If you rank/seed by TEST, over hundreds of rounds it turns into an optimization target, and the result
looks unrealistically optimistic — so you must not do that.

Controlled by: `ALPHANODE_SEED_FROM_LIBRARY` (1/0), `ALPHANODE_EXPLORE_EVERY` (N).

## Desktop interface (minimal, native)

```bash
.venv/bin/python alphanode/alphanode_gui.py
```
A CustomTkinter window, light or dark (the switch is in the header; it follows the OS on first run).
On the left — the **full set of search settings** (scrollable), grouped into sections:
- **Resources / universe** — CPU slider (5–95% → workers), the set of pairs;
- **Search** — population, generations, seed, pause, port;
- **Node mode** — `EXPLORE_EVERY` (how often to explore from scratch), warm-start on/off,
  max rounds, leaderboard size;
- **Simulation** — target volatility, fee;
- **Genome** — max formula depth/size;
- **Selection (GA)** — tournament, elitism, random injection, crossover fraction;
- **Fitness** — complexity penalty, correlation threshold/penalty, Hall of Fame size;
- **Date segments** — TRAIN / VAL / TEST boundaries.

On the right — live status, a **progress chart** (best TEST by round) and a **leaderboard** of the best
alpha from each family. The **"rank by: fitness / TEST OOS"** toggle above the table changes the ordering:
by default it is **by the honest fitness `min(train,val)`** (the same criterion the node selects on), or
**"top by TEST OOS"** — just to see who does best on held-out data. ⚠ The second mode is a
**cherry-pick on held-out**: alphas chosen this way have an inflated TEST (a selection effect), so it is
for viewing, not a selection criterion (as the caption warns). Next to it — a **"TEST >"** field:
a threshold on held-out Sharpe (e.g. `1` — keep only alphas with TEST OOS > 1; empty — no filter,
Enter/leaving the field applies it). The TEST filter works in both ranking modes. The
**trades L/S** column (total number of long / short positions OPENED over the TEST period — a trade = crossing into
long/short from flat or the opposite side) and **win%** (the share of days with a profit, daily hit-rate) are computed
**on TEST (OOS)** for the current data and are filled in in the background (counted on target weights; the strategy
rebalances daily, so high counts mean high turnover).

**Download the table** — the **CSV** button in the leaderboard heading saves what is on screen: the same
rows in the same order, with fitness, TRAIN/VAL/TEST Sharpe, TEST drawdown/CAGR, the trade stats and the
formula (a stat still being computed, or one that failed, comes out blank). The right-click menu has both
that and **"Export full library (CSV)"** — *every* alpha the node has ever mined (no dedup, no `TEST >`
filter), ordered by honest fitness, with all four numbers (sharpe/dd/cagr/n) per segment. The table on
screen is a deliberately diverse *slice* of the library; the second export is the whole thing.

A **double-click on a leaderboard row** opens a window with the alpha's equity curve
(growth of $1, log scale) with **TRAIN | VAL | TEST** zones and a comparison against a **buy & hold (EW)** basket —
the same style as `evo_champions.png`.

At the bottom — a **PORTFOLIO** panel: **▶ Build portfolio** runs the top-N alphas by TEST Sharpe
through the project's real `Portfolio` engine (`quantpylib`), in parallel in the background (~1–2 min),
and shows the **combined dollar-neutral equity on TEST** + Sharpe / CAGR / MaxDD vs a buy & hold basket.
Diversifying across decorrelated alphas beats any single one and rides through market crashes.
⚠ Selecting by TEST inflates the number (cherry-pick on held-out); the diversification gain is the
robust part. (Headless: `python alphanode/portfolio_build.py --top 6`.)

Buttons: **▶ Run / ■ Stop / Reset to
defaults**. Settings
are saved to `gui_settings.json`; on start they are passed to the node as `ALPHANODE_*` variables. The GUI
launches `node.py` as a subprocess and reads its status. It needs a graphical display + `python3-tk`
(usually present on the system; tkinter is already available in the venv).

## Desktop build (installable on other machines)

The app is packaged into a self-contained binary (Python and dependencies are inside — nothing needs to be
installed on the target machine):

- **Ubuntu/Debian** → `alphanode_*.deb` (double-click / `apt`, menu entry): `bash packaging/build_deb.sh`
- **Any Linux** → `AlphaNode-x86_64.AppImage` (single file): `bash packaging/build_linux.sh`
- **Windows** → `AlphaNode-Setup.exe` (installer) + portable zip — via CI
  ([`.github/workflows/build.yml`](../.github/workflows/build.yml), run manually or by tag `vX.Y.Z`).

Details, internals (the `--role node/fetch/signal/metrics/runpy` roles, the user data folder) and manual
builds — in [`packaging/README.md`](../packaging/README.md). The run-from-source mode is unchanged.

## CLI (no GUI — for server/ssh/Docker)

All node control from the terminal — [`alphanode/cli.py`](cli.py). Subcommands:

```bash
python alphanode/cli.py run [flags]     # continuous search in foreground (log to stdout)
python alphanode/cli.py fetch [flags]   # download fresh Binance data
python alphanode/cli.py top [flags]     # top alphas found, as a table in the terminal
python alphanode/cli.py status          # node state (rounds, best)
python alphanode/cli.py export [flags]  # assemble a paper-trading bundle from a formula/rank
```

- **`run`** — the same settings as in the GUI, but as flags: `--cpu 50 --pop 200 --gens 25 --universe all
  --max-rounds 0 --port 8787 --state-dir /data …` (an unset flag = from `ALPHANODE_*`/`config.ini`).
- **`top`** — like the GUI leaderboard: `--sort fitness|test`, `--min-test 1` (threshold on TEST OOS), `-n 20`,
  `--all` (no family dedup). ⚠ `--sort test` is a cherry-pick on held-out (the number is inflated).
- **`export`** — `--rank N --sort test` (the N-th alpha) or `--formula "cs_…"`; puts the bundle in `exports/`.

It reads/writes state in `ALPHANODE_STATE_DIR` (in Docker — `/data`), so `top`/`status`/`export`
see the library of a running node. The compiled binary can also do CLI: `AlphaNode --role cli top`.

## Running in Docker (CLI-first, for "always in the background")

The image is CLI: entrypoint `cli.py`, default command `run` (continuous search).

```bash
cd alphanode
docker-compose up --build -d     # bring the node up in the background (run command)
# status:  http://localhost:8787
docker-compose logs -f           # what it's doing

# same /data, without disturbing the running node:
docker compose run --rm alphanode top --sort test --min-test 1
docker compose run --rm alphanode status
docker compose run --rm alphanode fetch --top 150 --min-years 3
docker compose down              # stop
```

Build/run the image directly (on any machine with Docker):
```bash
docker build -f alphanode/Dockerfile -t alphanode .        # from the repo root
docker run -v alphanode-data:/data -p 8787:8787 alphanode  # node
docker run -v alphanode-data:/data alphanode top --sort test   # view the results
```

## Running locally (without Docker)

```bash
# from the repo root, needs a .venv with numpy/pandas
python alphanode/cli.py run --cpu 50 --pop 200 --gens 25        # CLI (recommended)
# or the node directly via the env file:
env $(grep -v '^#' alphanode/alphanode.env | xargs) .venv/bin/python alphanode/node.py
```

## Resource control (10–90%)

`ALPHANODE_CPU_PERCENT` (5–95) → number of parallel workers = `% × cores`
(e.g. 50% on 12 cores → 6 workers). The node runs at a background priority (`nice`).

In Docker there is also a **hard cap** at the container level — `cpus:` in `docker-compose.yml`.
Set it to match your percentage (50% of 12 cores → `cpus: "6"`). It keeps the node from exceeding the limit,
even if something goes wrong.

## What's configurable

**EVERYTHING** the engine understands is configurable — in three equally valid ways:
- **GUI** — by hand in the left panel (see the sections above), saved to `gui_settings.json`;
- **`alphanode.env`** — the same keys as `ALPHANODE_*` variables (for CLI/Docker);
- **`../evolution/config.ini`** — the default baseline.

Priority: `ALPHANODE_*` (env/GUI) **overrides** `config.ini`. For an empty/unset variable, the
value is taken from `config.ini`. The full list of keys — in `alphanode.env`:
CPU %, universe, pop/gens/seed/pause/port, `EXPLORE_EVERY`, warm-start, max rounds,
leaderboard, `TARGET_VOL`, `EXEC_COST`, genome (`MAX_DEPTH`/`MAX_SIZE`), GA (`TOURNAMENT`/
`ELITISM`/`RANDOM_INJECT`/`CROSSOVER_PROB`), fitness (`PARSIMONY`/`CORR_THRESHOLD`/`CORR_PENALTY`/
`HOF_CAPACITY`), date segments (`TRAIN_START`/`VAL_START`/`TEST_START`/`TEST_END`).

## Paper trading (exporting a strategy)

Double-click an alpha → in the equity window, the **"📄 Paper Trade — assemble bundle"** button. It generates
a **self-contained folder** `exports/paper_<hash>/` that you can take away and run anywhere:

- `strategy.py` — the formula as a strategy class (through the real `run_simulation` engine);
- `paper_trade.py` — a daily paper trader on **live Binance data** (urllib, no keys needed):
  pulls closed daily candles → target positions → mark-to-market → rebalance → fees → log;
- `README.md`, `config.json`, `requirements.txt` (numpy, pandas);
- a copy of the engine (`quantpylib/` + `primitives/genome/evaluator/fastsim/evolved_strategy`) — **with no
  dependency on the repository**.

In the dialog: **📂 Open folder** and **▶ Run now** (a step straight from the app, log in a popup).
The account accumulates in `paper_state.json`, trades in `paper_trades.csv`. Run it once a day after
00:00 UTC (a cron example is in the bundle's README).

Next to it — **"📥 Download signals (CSV)"**: exports a readable positions table for this strategy
(one row = one position): `date, segment, ticker, side, weight, weight_pct` — **LONG/SHORT** and
the portfolio share as a percentage (`+8.9%` / `−15.8%`), sorted by day and by size. After saving,
a **"what to hold now"** window pops up — the last day's positions as a list. It is computed by the same engine
as the equity chart.

⚠️ This is paper and a hypothetical backtest. **First — a forward-test on NEW data** (weeks/months),
and only then real money in small size. Live execution is deliberately not part of the bundle
(it needs keys, limits, a kill-switch).

## Data (Binance)

By default the search runs over `data.pickle` (a snapshot of 30 coins). To fetch by a **live
universe**, update the data — it takes the **top-N USDT perps by 24h turnover** among those whose
**history is ≥ N years** (young listings are cut off by `onboardDate` — otherwise you'd get almost solid NaN
in the search window, and the data fetcher would be crawling the pre-listing period one day at a time):

```bash
python fetch_data.py --top 150 --min-years 3     # overwrites data.pickle (atomically)
python fetch_data.py --top 100 --min-years 2 --start 2019-09-05
```
Or the **"⟳ Update data.pickle"** button in the GUI (fields "Pairs (top by turnover)" = N and
"Min. history (years)"; the fetch log is in a popup). Public Binance endpoints, no keys needed.
Each pair is fetched from its own listing date (no day-by-day crawl). After an update, `all` =
all fetched pairs.

Caution: these are **survivor** coins (Binance doesn't return delisted ones) — survivorship remains;
more pairs → each search round proportionally slower. After changing the data, **clear the history**
(a button in the GUI) and restart the search — the old library was searched on a different universe.

## What comes out (the `/data` volume, locally — `alphanode/state/`)

- `library.jsonl` — the **accumulated library** of all alphas found (deduped by formula,
  with train/val/TEST metrics and a round number). It grows every round and survives a restart.
- `status.json` — the current state (rounds, formulas explored, top alphas).
- The status page `http://localhost:PORT` — cards + a leaderboard of the best by TEST, auto-refresh.

## Roadmap (groundwork)

- Rotating the universe across rounds (mining alphas for different sets of pairs).
- Handing a champion to paper-trade straight from the node (the `evolved_strategy.make_evolved` bridge).
- A cluster of nodes (several machines writing to a shared library).

Disclaimer: the metrics are a hypothetical backtest, not investment advice. What you find must be verified
with a forward-test on NEW data.
