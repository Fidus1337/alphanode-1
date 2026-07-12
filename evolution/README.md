# Evolutionary strategy search

Genetic programming on top of the `quantpylib` engine. It searches a huge
space of alpha-signal formulas, runs each one through the **same** engine as the
hand-written strategies (`strategies.py`), and surfaces robust champions.

## The idea in one paragraph

The entire "DNA" of a strategy in this engine is a single thing: a formula that turns OHLCV into a
per-instrument `alpha` signal. Everything else (inverse-vol, forecast normalization,
vol-targeting, position inertia, fees) is the engine's fixed machinery. So what you need to
evolve is exactly that formula. The grammar covers all the hand-written strategies:
`ts_zscore(close,14)` is Bollinger, `sign(sub(ts_mean(close,10),ts_mean(close,20)))`
is MAverage. That means the search happens in a space that **strictly contains** your
current work, and can beat it.

## How it's built

| File | Role |
|------|------|
| `primitives.py` | Operator dictionary (38 ops): time-series unary (`ts_mean/std/zscore/rank/argmax/argmin/median/skew/kurt/roc/delta/delay/sum/min/max/ema/decay_linear`), time-series binary (`ts_corr/ts_cov`), element-wise (`add/sub/mul/div/pmin/pmax/gt/lt/neg/sign/abs/slog/tanh/sigmoid/ssqrt`), cross-sectional (`cs_rank/zscore/demean/scale`). Terminals: OHLCV + `ret` and derived `vwap/range/body/dvol/logret` (built in `evaluator.add_derived_features`, shared with the live export). Windows `[2..200]`. All on wide tables, past-only. |
| `genome.py` | Genome = an expression tree. Random generation, crossover (swapping subtrees), mutations (subtree / operator / window). |
| `evaluator.py` | Compile tree → wide signal table → lightweight `PrecomputedAlpha(Alpha)` → `run_simulation()`. Metrics per segment from the `capital` column (NET, after fees). |
| `evolution.py` | The GA loop: tournament selection, elitism, random injection, formula cache, parallel evaluation, a diverse Hall of Fame. |
| `run_evo.py` | Config, run, final honest evaluation on the held-out TEST, `champions.json`, charts. |
| `report.py` | Progress charts and champion equity with TRAIN\|VAL\|TEST zones. |

## Discipline against overfitting

Searching through millions of formulas is a perfect machine for data snooping. So:

1. **Three segments.** TRAIN (evolution) → VAL (robustness) → **TEST held-out** and touched
   exactly once at the end.
2. **Fitness = `min(train_Sharpe, val_Sharpe)`.** A strategy is only as good as
   its *worst* training segment → it is rewarded for robustness, not for fitting.
3. **Complexity penalty** (parsimony) → simple beats complex.
4. **Dedup/penalty for correlation** with champions already found → the Hall of Fame is diverse,
   not 12 clones of one MA-cross.
5. **Degeneracy filter** → the signal must actually trade (≥10% of days) in train and val.
6. **Multiple-testing correction.** The report prints the number of formulas explored:
   the best TEST-Sharpe should be read with a discount. TEST is an estimate, not a guarantee;
   the real check is a forward/paper run on NEW data.

## Settings

All parameters — in [config.ini](config.ini) (read via `config.py`, stdlib, no dependencies):
target volatility, fee, population size, number of generations, seed, number of cores,
formula depth/size, selection and fitness parameters, TRAIN/VAL/TEST segment boundaries,
and the **universe of pairs** (`[universe] instruments = all` or a comma-separated list). The same config
is used by `validate_champions.py` too, so vol/fees/segments/pairs don't diverge between
search and validation. CLI flags override the file.

## Running

```bash
cd evolution
../.venv/bin/python run_evo.py                   # full run per config.ini
../.venv/bin/python run_evo.py --smoke           # quick smoke test (1 core)
../.venv/bin/python run_evo.py --pop 300 --gens 40 --jobs 10 --seed 3   # override on the fly
../.venv/bin/python run_evo.py --config my.ini   # a different config file
```

Artifacts: `champions.json` (formulas + per-segment metrics), `evo_progress.png`
(quality growth by generation), `evo_champions.png` (equity of the top champions).

## Inspection tools

```bash
python show_formula.py "<formula>" [--signal BTCUSDT ETHUSDT]  # one formula: metrics+equity(+signal)
python plot_champions.py [--by test|base] [--top N]            # redraw evo_champions.png
python champion_entries.py [--rank N | --top K | --all]        # table of asset entries on TEST (with PnL)
python validate_champions.py [K]                               # top-K through the REAL engine
```

## Experiment database and reuse

Every run is appended to the permanent log `experiments/registry.jsonl` (set of pairs, config,
champions) — while `champions.json` stays the "current working set". All formulas ever found
form a **library** that can be reused in two ways:

```bash
# 1) RE-SCORING: run the whole library on a new set of pairs in seconds (no evolution)
python library_rescore.py --universe BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT

# 2) WARM-START: seed the evolution with known champions -> converges faster
python run_evo.py --seed-from-library [--seed-source ALL|'BTC|ETH|...']
```

**Test hygiene:** selection and ranking — only by `min(train,val)`; TEST is printed for reference.
Each re-scoring increments the `experiments/test_peeks.json` counter — this is the debt of multiple
testing (the more peeks, the bigger the discount on the best TEST; the final truth is a forward-test).

## Speed

The daily loop of the `run_simulation` engine on pandas `.at[]` is ~32 s/genome — for searching
thousands of formulas that is unacceptable. `fastsim.py` is a numpy port of the same loop (~0.2 s/genome,
×110), verified to match the engine (`verify_fastsim.py`, corr ≥ 0.99). It
is used as the FITNESS; champions are then rechecked on the real engine.

## Deploying a champion

`evolved_strategy.make_evolved(formula)` turns a formula string into an ordinary
`Alpha` class (through the real engine) — you can drop it into `eval_strategies.py`,
`eval_oos.py`, `paper_trade.py` on a par with Bollinger/RSI:

```python
from evolution.evolved_strategy import make_evolved
Champ = make_evolved('div(ts_delta:30(open),ts_sum:100(cs_scale(ret)))', 'Champ5')
# then like any strategy: Champ(insts=..., dfs=..., portfolio_vol=0.30, execrates=0.001)
```

`validate_champions.py [K]` runs the top-K from `champions.json` through the REAL engine and
cross-checks against fast-sim (proof that the proxy and the bridge are honest).

## What's next

- Drop a champion into `paper_trade.py` via `make_evolved` and run it forward.
- Forward validation: re-select as new data arrives (walk-forward).
- Remember the multiple-testing correction: the more formulas explored,
  the more optimistic the best TEST-Sharpe. An ensemble of robust champions is more reliable than one.
