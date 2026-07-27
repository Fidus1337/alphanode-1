# Neuro-symbolic advisor (experiment) — LLM-guided mutations

Branch: `feat/neuro-advisor`. Status: implemented, plumbing verified; awaiting live A/B.

## The idea

The GP search mutates blindly: a random node is replaced by a random subtree. FunSearch-style
neuro-symbolic evolution replaces part of that blindness with an informed proposer: when the
search **plateaus**, an LLM sees the current best formulas + the Hall of Fame and proposes new
formulas in the same DSL, each encoding a distinct economic hypothesis (carry via `funding`,
flow, vol-structure, ...).

The division of labor is strict and is what keeps the search honest:

> **The LLM only proposes. The simulator judges.**

Proposals enter the population as ordinary genomes — evaluated by fast_sim on TRAIN/VAL,
scored by `min(train, val)` Sharpe, deduplicated and correlation-penalized like everything
else. A bad idea dies in selection exactly like a bad random mutation. The LLM never sees
market data, cannot look ahead, and cannot inflate a score.

## How it works (code)

* `evolution/advisor.py` — `Advisor.propose()` calls the Claude API (`claude-opus-5` by
  default) with the DSL grammar + current tops + HoF, gets a JSON-schema-constrained list of
  `{hypothesis, formula}`, and strictly validates each against the DSL (unknown ops/features,
  arity, window rules, size/depth — hallucinations are dropped before they waste a slot).
* `evolution/evolution.py` — a plateau detector (no best-fitness improvement for `patience`
  generations) triggers a consult; valid proposals take the random-injection slots of the next
  generation. Every injected formula is tagged `origin='llm'`; the run ends with
  `advisor: N calls, P proposed, V valid -> X/Y of the Hall of Fame is LLM-born`.
* Fail-safe: no `anthropic` package, no credentials, network/auth errors, refusals — one log
  line, and evolution continues exactly as before. With `enabled = false` (the default) the
  behavior is bit-identical to the plain GA (verified: same seed -> same champions).

## Enabling

**GUI:** settings → *NEURO ADVISOR (LLM, experimental)* — tick the checkbox, paste the key
from console.anthropic.com, press *Check key* (a free auth-probing call), Start. The key is
stored in its **own file with 0600 permissions** (`alphanode/anthropic_key`, gitignored;
in the packaged app — the user data dir), never in `gui_settings.json` — that file is
tracked by git and a key there would leak with the next settings commit.

**CLI:**

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # or `ant auth login`
.venv/bin/python evolution/run_evo.py --advisor            # one run
# or persistently: evolution/config.ini [advisor] enabled = true
# or via env (also reaches the node's workers): ALPHANODE_ADVISOR=1
```

Cost control: the number of consults per run (= per node round) is hard-capped. Default 8;
one consult is roughly $0.05–0.15 on claude-opus-5. Set it to 1 for "at most one consult per
round", 0 to disable consults entirely. Ways to set it, most convenient first:

* **GUI:** *NEURO ADVISOR → Max LLM consults per round* (passed to the node on Start);
* **CLI:** `run_evo.py --advisor --advisor-max-calls 1`;
* env `ALPHANODE_ADVISOR_MAX_CALLS=1` or `evolution/config.ini` `[advisor] max_calls`.

## The experiment (what would make this a success)

A/B with everything held fixed except the advisor:

```bash
.venv/bin/python evolution/run_evo.py --seed 7             # control
.venv/bin/python evolution/run_evo.py --seed 7 --advisor   # treatment
```

Compare: (1) best-fitness trajectory per generation, (2) the `LLM-born` share of the final
Hall of Fame, (3) later — the TEST Sharpe of LLM-born vs GA-born champions after fresh
mining. Success = LLM-born champions appear with comparable-or-better TEST at less compute.

Known risk: the LLM is biased toward textbook factors (momentum/carry/reversal), which could
*reduce* diversity vs blind search. The corr-penalty and the "stay decorrelated from the HoF"
instruction push against that, but the A/B is the only honest answer.
