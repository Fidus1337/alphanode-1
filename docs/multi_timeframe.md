# Multi-timeframe trading (15m → 1d) — design

**Goal:** let the search + simulation + paper-trading run on any bar size from 15 minutes to 1 day,
not just daily. Branch: `dev/multi-timeframe`.

> Status 2026-07-26: shipped for 15m/1h/4h/1d. 5m was dropped from the product entirely — the
> data is too heavy and unmodeled microstructure dominates at that scale (see "Costs & realism"
> below, which predicted exactly this). The engine remains bar-size-agnostic; the 5m analysis is
> kept for the record.

## Key insight — this is *parameterizing the time axis*, not a rewrite

Almost everything is already timeframe-agnostic and reused as-is:
- **Primitives** (`primitives.py`) — operate on a wide table; windows are in *bars*, not days.
- **Genome / GA / DSL** (`genome.py`, `evolution.py`) — untouched.
- **The sim loop** (`fastsim.py` / real `quantpylib` engine) — steps bar-by-bar; only the
  annualization and vol constants are daily-tuned.
- **Report / GUI / leaderboard / portfolio** — mostly untouched (labels only).

The whole "daily" assumption lives in a handful of constants:

| File | Line | Daily assumption | Fix |
|------|------|------------------|-----|
| `evaluator.py` | `ANN = 365` | bars/year | `ANN = periods_per_year(tf)` |
| `evaluator.py` | `pd.date_range(freq='D')` | daily grid | `freq = pandas_freq(tf)` |
| `evaluator.py` | sharpe/cagr use `ANN` | — | derives from above |
| `fastsim.py` | `TARGET_ANN = 365` | vol-target annualization | `periods_per_year(tf)` |
| `fastsim.py` | `rolling(30)` vol window | ~1 month in daily bars | `vol_window(tf)` (bars) |
| `fastsim.py` | `EWMA_LAMBDA = 0.06` | vol-EWMA decay/bar | retune per tf (wall-clock half-life) |
| `quantpylib/simulator/alpha.py` | `sqrt(ewma*365)`, `rolling(30)` | same two, in the REAL engine | same fixes |
| `portfolio_build.py`, `verify_fastsim.py`, `alphanode_gui.py` | `sqrt(365)`, `/365` | metric annualization | shared `periods_per_year(tf)` |
| `fetch_data.py` | fetches 1d bars via the qt wrapper | data source | `--interval` param |
| `paper_export.py` | live klines `interval=1d`, runs once/day | paper loop | interval + sub-daily scheduler |

## Core abstraction: a `Timeframe`

One object carries everything derived from the bar size:

```python
Timeframe(name='1h',
          binance_interval='1h',      # for fetch + live klines
          pandas_freq='1h',           # build_panel grid
          seconds=3600,
          periods_per_year=8760,      # 24 * 365  -> Sharpe = mean/std * sqrt(ppy)
          vol_window=720,             # ~1 month of bars for vol estimation (a knob)
          ewma_lambda=...)            # retuned so half-life ≈ wall-clock target
```

`periods_per_year = 86400/seconds * 365`:
1d→365 · 4h→2190 · 1h→8760 · 15m→35040 · 5m→105120.

Thread a `tf` through `config.ini [timeframe]` → `build_panel` / `precompute_market` / `fast_sim` /
metrics. Default `1d` reproduces today's numbers exactly (regression guard).

## Honest risks (read before building)

1. **Fitness speed is the real bottleneck.** The `fast_sim` loop is O(bars). Daily ≈ 2.5k bars
   (~0.05 s/genome). 5m ≈ 525k bars/5y → ~200× → **~10 s/genome** → a 6000-genome run = ~16 h.
   Mitigations: further-vectorize the loop, subsample history for fitness, or pilot on 1h/4h
   (≈ 5–35k bars) where it stays fast. **Recommendation: validate the parameterization on 1h/4h
   first, push to 5m only after the speed path is solved.**
2. **Costs dominate intraday.** More bars → more turnover → fees + slippage + funding eat the edge.
   A daily backtest ignores microstructure; a 5m one *must* model realistic slippage or it lies.
   The project's honesty discipline matters MORE here, not less.
3. **Overfitting is worse.** More bars = more apparent significance on pure noise; alpha decays
   fast intraday. Keep TRAIN/VAL/TEST held-out; consider bar-count-aware multiple-testing discount.
4. **Data volume/storage.** 5m × 50 symbols × 5y ≈ 26M rows; `data.pickle` won't scale — move to a
   per-timeframe store (parquet per symbol, or one pickle per tf). Wide panel ≈ 1–1.5 GB in RAM.
5. **Paper/live loop.** Daily = run once/day. 5m = a scheduler on closed 5m bars (poll or websocket),
   closed-bar detection, and much tighter latency/fill assumptions.

## Phased roadmap

- **P0 — branch + this doc.** ✅ (`dev/multi-timeframe`)
- **P1 — data:** `fetch_data.py --interval {5m,15m,1h,4h,1d}` → per-tf store. Start with 1h.
- **P2 — engine parameterization:** `Timeframe` object + thread `tf` through build_panel /
  precompute_market / fast_sim / metrics. Regression test: `tf=1d` reproduces current numbers bit-for-bit.
- **P3 — speed:** benchmark fast_sim at the target tf; vectorize / subsample if needed.
- **P4 — search + validate:** GP on 1h, sanity vs daily, then extend to 4h / 15m / 5m.
- **P5 — paper/live:** intraday paper trader (closed-bar scheduler, realistic costs).
- **P6 — GUI:** timeframe selector in the node panel + leaderboard/label plumbing.

## Open decisions (to confirm)

- **Pilot timeframe** — where to prove the parameterization first (1h recommended: enough bars for
  stats, still fast).
- **Fitness speed strategy** — vectorize the loop vs subsample history vs stay ≥1h.
- **Cost model** — flat bps (today) vs bps + slippage-vs-volume for intraday realism.
- **Data store** — parquet-per-symbol vs one pickle per tf.
