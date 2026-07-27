"""Bridge: a champion formula -> a regular Alpha class compatible with strategies.py.

Lets you take a string from champions.json and use it like any hand-written strategy
(in eval_strategies.py, eval_oos.py, paper_trade.py) — through the REAL run_simulation engine.

    from evolved_strategy import make_evolved
    Champ = make_evolved('ts_roc:14(add(ts_sum:3(ts_max:20(mul(volume,open))),high))', 'Champ0')
    a = Champ(insts=tk, dfs=fresh(...), start=..., end=..., portfolio_vol=0.30, execrates=0.001)
    port = a.run_simulation()
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
PROJ = os.path.dirname(HERE)
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

from quantpylib.simulator.alpha import Alpha    # noqa: E402
from genome import parse                        # noqa: E402
from evaluator import eval_alpha_panel, add_derived_features   # noqa: E402


class EvolvedAlpha(Alpha):
    """Computes alpha from the genome formula on the wide panel assembled from self.dfs."""
    FORMULA = None

    def __init__(self, formula=None, **kwargs):
        super().__init__(**kwargs)
        self.formula = formula or self.FORMULA
        assert self.formula, 'a formula is required (formula argument or FORMULA attribute)'
        self._node = parse(self.formula)

    def pre_compute(self, date_range):
        pass

    def compute_forecasts(self, date, eligibles):
        return {inst: self.dfs[inst].at[date, 'alpha'] for inst in eligibles}

    def post_compute(self, date_range):
        # by this point the engine has already reindexed self.dfs and computed ret/vol/eligible
        base = ['close', 'open', 'high', 'low', 'volume', 'ret']
        panel = {f: pd.DataFrame({inst: self.dfs[inst][f] for inst in self.insts})
                 for f in base}
        # look-ahead guard: the engine bfills the price into the pre-listing region; we zero out
        # the base features before the first eligible date so cross-sectional operators can't see
        # the future (consistent with the ffill-only search panel in build_panel).
        for inst in self.insts:
            ok = self.dfs[inst]['eligible']
            ok = ok[ok > 0].index
            if len(ok):
                cut = ok.min()
                for f in base:
                    panel[f].loc[panel[f].index < cut, inst] = np.nan
        add_derived_features(panel)      # derived terminals from the masked base -> same masking
        # funding terminal: from the dfs when the data has it, zeros otherwise (a formula with
        # `funding` then yields a zero/constant signal instead of crashing the live bridge).
        # Same pre-listing masking as the base features (funding is 0.0-filled inside the range).
        panel['funding'] = pd.DataFrame(
            {inst: (self.dfs[inst]['funding'] if 'funding' in self.dfs[inst].columns
                    else pd.Series(0.0, index=self.dfs[inst].index)) for inst in self.insts}
        ).fillna(0.0).where(panel['close'].notna())
        alpha_wide = eval_alpha_panel(self._node, panel)
        for inst in self.insts:
            a = alpha_wide[inst].reindex(self.dfs[inst].index).ffill()
            self.dfs[inst]['alpha'] = a
            self.dfs[inst]['eligible'] = self.dfs[inst]['eligible'] & (~pd.isna(a))


def make_evolved(formula, name='Evolved'):
    """Factory: return an Alpha subclass with the formula baked in (like Bollinger, etc.)."""
    return type(name, (EvolvedAlpha,), {'FORMULA': formula})
