"""Round ANALYST: after a search round, an LLM reads the accumulated evidence — the alpha
library with its metrics, the round history, operator/feature usage — and returns a
structured research report: what the library really contains, where overfitting is likely,
what is under-explored, what to try next.

This replaced the old formula-proposing advisor (removed 2026-07): injecting one-shot LLM
formulas into the GA added little over random injection — the model's comparative advantage
is READING evidence, not guessing formulas. The analyst has ZERO authority over the search:
it never scores, never selects, never mutates. Its report goes to the human (LIVE LOG,
status page, analysis journal) and nowhere else.

Driven by node.py at round boundaries (in a background thread — the next round never waits).
Needs the `anthropic` SDK + ANTHROPIC_API_KEY; without them the node runs exactly as before.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEFAULT_MODEL = 'claude-opus-5'

# The response is constrained to this schema — no free-text parsing, the API validates.
_SCHEMA = {
    'type': 'object',
    'properties': {
        'summary': {'type': 'string'},
        'findings': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'severity': {'type': 'string', 'enum': ['info', 'warn', 'critical']},
                    'title': {'type': 'string'},
                    'detail': {'type': 'string'},
                },
                'required': ['severity', 'title', 'detail'],
                'additionalProperties': False,
            },
        },
        'suggestions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string'},
                    'detail': {'type': 'string'},
                },
                'required': ['title', 'detail'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['summary', 'findings', 'suggestions'],
    'additionalProperties': False,
}

_SYSTEM = """You are the research analyst of an evolutionary alpha-search node for crypto \
perpetuals. After each search round you receive the node's accumulated evidence as JSON:

  * champions — the alpha library (formula, base = min(TRAIN,VAL) Sharpe used for selection,
    held-out TEST Sharpe/drawdown, the round each entered);
  * base_test_corr — correlation between selection fitness and held-out TEST across the
    library (the single most important honesty number: negative or ~0 means the search
    optimizes something that does not transfer);
  * rounds — recent round history (best base, its TEST, mode refine/explore, library size);
  * usage — how often each operator/feature appears across library formulas;
  * config — timeframe, universe size, population/generations.

Your job is the analysis a disciplined quant would do, NOT idea generation:
  1. Overfitting forensics first: base vs TEST gaps and their trend, families that look
     great in-sample and die held-out, suspiciously spiky windows.
  2. Diversity audit: is the library one family wearing twenty masks? Which operator/feature
     clusters dominate; what is genuinely uncorrelated.
  3. Coverage gaps: features/operators barely used (e.g. funding, volume) that deserve rounds.
  4. Process health: refine vs explore balance, is the library bar rising or has it stalled.

Rules: cite the NUMBERS you were given (rounds, Sharpes, counts) — no generic advice; be
skeptical and terse; at most 5 findings and 3 suggestions, ordered by importance; severity
'critical' only for things that invalidate conclusions (e.g. negative base↔TEST correlation).
Suggestions must be actions the user can take in this product (change segments, universe,
timeframe, parsimony/correlation knobs, run explore rounds, distrust certain families) —
NOT new formulas and NOT code changes."""


class Analyst:
    """Thin, fail-safe wrapper: analyze(payload) -> report dict, or None (error in .last_error)."""

    def __init__(self, model=None, log=print):
        self.model = model or DEFAULT_MODEL
        self.log = log
        self.last_error = None

    def analyze(self, payload):
        try:
            import anthropic
            client = anthropic.Anthropic()         # key from env or an `ant auth login` profile
            resp = client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=_SYSTEM,
                output_config={'format': {'type': 'json_schema', 'schema': _SCHEMA}},
                messages=[{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
            )
            if resp.stop_reason == 'refusal':
                self.last_error = 'request refused'
                return None
            text = next(b.text for b in resp.content if b.type == 'text')
            rep = json.loads(text)
        except Exception as e:                     # noqa: BLE001 — never crash the node
            self.last_error = f'{type(e).__name__}: {str(e)[:110]}'
            try:
                import anthropic
                if isinstance(e, anthropic.AuthenticationError):
                    self.last_error = 'API key invalid (401)'
            except Exception:                      # noqa: BLE001
                pass
            self.log(f'analyst: call failed ({self.last_error})')
            return None
        rep['findings'] = rep.get('findings', [])[:5]
        rep['suggestions'] = rep.get('suggestions', [])[:3]
        return rep
