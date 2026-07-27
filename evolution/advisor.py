"""Neuro-symbolic advisor: an LLM proposes formulas when the GP search stalls.

Division of labor is strict — THE LLM ONLY PROPOSES, THE SIMULATOR JUDGES. Proposals are
plain DSL formulas that enter the population like any other genome: they are evaluated by
fast_sim on TRAIN/VAL, scored by the same honest fitness, and die in selection if they are
bad. The advisor never sees market data, cannot look ahead, and cannot inflate a score —
it only replaces part of the *blind* random mutation with an informed guess.

Wire-up (see evolution.evolve): when the best fitness hasn't improved for `patience`
generations, evolve() sends the advisor the current top formulas + their metrics + the
Hall-of-Fame it must stay decorrelated from, and injects the valid proposals into the
next generation, tagged origin='llm' so the experiment is measurable.

Requires the `anthropic` SDK and credentials (ANTHROPIC_API_KEY env, or a profile from
`ant auth login`). When either is missing, `Advisor.available` is False and evolution
runs EXACTLY as before — the advisor is a pure add-on, never a dependency.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import primitives as P                      # noqa: E402
from genome import parse                    # noqa: E402

DEFAULT_MODEL = 'claude-opus-5'

# The response is constrained to this schema — no free-text parsing, the API validates.
_SCHEMA = {
    'type': 'object',
    'properties': {
        'proposals': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'hypothesis': {'type': 'string'},
                    'formula': {'type': 'string'},
                },
                'required': ['hypothesis', 'formula'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['proposals'],
    'additionalProperties': False,
}

_SYSTEM = """You are the mutation advisor inside a genetic-programming search for \
cross-sectional crypto-perp alpha signals. The search is stuck on a fitness plateau; \
your job is to propose NEW candidate formulas that explore hypotheses the blind random \
search is unlikely to stumble on. Every proposal will be simulated on held-out-honest \
data and scored as min(TRAIN Sharpe, VAL Sharpe); bad ideas die in selection — so be \
bold and DIVERSE rather than safe and similar.

THE DSL (this grammar is exact — anything else fails to parse):
  formula   := feature | op(args)
  feature   := one of: {features}
  windowed time-series ops (SUFFIX the window with a colon, e.g. ts_mean:20(close)):
    {un_ts}   with windows from: {windows}
  element-wise unary ops (no window): {un_elem}
  binary ops (no window, two args): {binary}
  cross-sectional ops (no window, rank/normalize ACROSS coins on each bar): {un_cs}

Semantics you must respect:
  * data is a wide table (rows = bars, columns = coins); ts_* ops look BACK along time
    within each coin; cs_* ops compare coins against each other on the same bar.
  * the output is a signal: positive = long, negative = short; the engine cross-sectionally
    normalizes it, so only the RELATIVE ordering across coins matters.
  * 'funding' is the perp funding rate paid within the bar (longs pay when positive) —
    carry/squeeze hypotheses live here. 'volume'/'dvol' = activity, 'ret' = bar return.
  * complexity is penalized: prefer trees of size 3-{max_size} nodes, depth <= {max_depth}.

Rules:
  1. Output ONLY formulas in the exact DSL above. No new operators, no numeric literals,
     no features not in the list. Windows must come from the allowed list.
  2. Each proposal must encode a DIFFERENT economic hypothesis (carry, flow, reversal,
     vol-structure, liquidity, momentum-quality, ...). State the hypothesis in one line.
  3. Stay DECORRELATED from the Hall of Fame formulas you are shown — do not rephrase
     them; attack from angles they do not cover.
  4. Windowed ops use `op:window(arg)` syntax, binary ops `op(a,b)`, e.g.:
     neg(cs_rank(ts_mean:30(funding)))   div(ts_std:10(ret), ts_std:60(ret))"""


def _fmt_top(top):
    return '\n'.join(f'  fit {t["fit"]:+.2f} train {t["train"]:+.2f} val {t["val"]:+.2f} '
                     f'size {t["size"]:2d}  {t["canon"]}' for t in top) or '  (none valid yet)'


def validate_formula(s, cfg):
    """Parse + strictly validate an LLM proposal -> Node, or None if it breaks the DSL.
    genome.parse is permissive (unknown ops only explode later, at eval) — here we reject
    them up front so a hallucinated operator never wastes a population slot."""
    try:
        node = parse(str(s).strip())
    except Exception:                              # noqa: BLE001 — not even parseable
        return None
    if not (1 < node.size() <= cfg['max_size'] and node.depth() <= cfg['max_depth']):
        return None
    for n in node.all_nodes():
        if n.is_terminal:                          # is_terminal == op in FEATURES
            continue
        if n.op not in P.ARITY or len(n.children) != P.ARITY[n.op]:
            return None
        if P.NEEDS_WINDOW[n.op] != (n.window is not None):
            return None
        if n.window is not None and not (2 <= int(n.window) <= 500):
            return None
    return node


class Advisor:
    """Thin, fail-safe wrapper around the Claude API. Any failure -> [] and a log line."""

    def __init__(self, model=None, n_proposals=10, log=print):
        self.model = model or DEFAULT_MODEL
        self.n = n_proposals
        self.log = log
        self._client = None
        self._dead = False           # set after an unrecoverable error (bad creds, no SDK)
        self.stats = {'calls': 0, 'proposed': 0, 'valid': 0}

    # ---------- availability ----------
    def available(self):
        if self._dead:
            return False
        return self._ensure_client() is not None

    def _ensure_client(self):
        if self._dead:
            return None
        if self._client is not None:
            return self._client
        try:
            import anthropic
            self._client = anthropic.Anthropic()   # key from env or an `ant auth login` profile
        except Exception as e:                     # noqa: BLE001 — no SDK / no creds -> advisor off
            self._dead = True
            self.log(f'advisor: unavailable ({type(e).__name__}: {e}) — running without it')
            self._client = None
        return self._client

    # ---------- the call ----------
    def propose(self, top, hof_canons, plateau_gens, cfg):
        """-> list of (Node, hypothesis) validated against the DSL and size limits.
        `top`: [{'canon','fit','train','val','size'}...] best current genomes."""
        client = self._ensure_client()
        if client is None:
            return []

        system = _SYSTEM.format(
            features=', '.join(P.FEATURES),
            un_ts=', '.join(P.UN_TS), un_elem=', '.join(P.UN_ELEM),
            binary=', '.join(P.BINARY), un_cs=', '.join(P.UN_CS),
            windows=', '.join(map(str, P.WINDOWS)),
            max_size=cfg['max_size'], max_depth=cfg['max_depth'],
        )
        user = (
            f'Timeframe: {cfg.get("tf", "1d")} bars. Fitness has not improved for '
            f'{plateau_gens} generations.\n\n'
            f'Current best genomes (fitness = min(train,val) Sharpe - parsimony):\n'
            f'{_fmt_top(top)}\n\n'
            f'Hall of Fame (stay DECORRELATED from these, do not rephrase them):\n'
            + ('\n'.join(f'  {c}' for c in hof_canons) or '  (empty)')
            + f'\n\nPropose {self.n} formulas, each testing a distinct hypothesis.'
        )

        try:
            self.stats['calls'] += 1
            resp = client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                output_config={'format': {'type': 'json_schema', 'schema': _SCHEMA}},
                messages=[{'role': 'user', 'content': user}],
            )
            if resp.stop_reason == 'refusal':      # safety classifiers said no — just skip
                self.log('advisor: request refused — skipping this consult')
                return []
            text = next(b.text for b in resp.content if b.type == 'text')
            raw = json.loads(text).get('proposals', [])
        except Exception as e:                     # noqa: BLE001 — network/auth/parse: never crash evolve
            import anthropic
            if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                              TypeError)):         # TypeError = SDK "could not resolve auth method"
                self._dead = True                  # credentials won't appear mid-run — stop retrying
            self.log(f'advisor: call failed ({type(e).__name__}: {e}) — continuing without it')
            return []

        out, seen = [], set()
        for p in raw:
            self.stats['proposed'] += 1
            node = validate_formula(p.get('formula', ''), cfg)
            if node is None:
                continue
            c = node.canon()
            if c in seen:
                continue
            seen.add(c)
            self.stats['valid'] += 1
            out.append((node, str(p.get('hypothesis', ''))[:120]))
        return out
