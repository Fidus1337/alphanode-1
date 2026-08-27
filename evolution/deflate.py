"""The noise floor: what the BEST of N tries scores when there is no edge at all.

A search that evaluates N formulas and reports the best one has not tested one
hypothesis — it has tested N and quoted the maximum. The maximum of N draws sits
well above zero even when every draw is luck, so "is this Sharpe above zero" is the
wrong question. The right one is "is it above the best that luck would have produced
anyway, given how hard we looked".

    E[max of N] ~= sd * [ (1-g) * Finv(1 - 1/N) + g * Finv(1 - 1/(N*e)) ]

with sd the spread of the score across the trials, g the Euler-Mascheroni constant
and Finv the inverse standard normal CDF (Bailey & Lopez de Prado, 2014). Two
properties matter in practice: the floor grows with the SPREAD of what the search
tried, and it grows only logarithmically with N — mining ten times longer raises the
bar a champion must clear, it does not lower it.

Everything here is stdlib on purpose: the frozen build must not gain a scipy import.
"""
import math

EULER = 0.5772156649015329

# Acklam's rational approximation to the inverse normal CDF (|rel. error| < 1.2e-9),
# which is far tighter than the input sd will ever be known to.
_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
      1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
      6.680131188771972e+01, -1.328068155288572e+01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
      -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
      3.754408661907416e+00)
_P_LOW = 0.02425


def norm_ppf(p):
    """Inverse standard normal CDF."""
    if not 0.0 < p < 1.0:
        raise ValueError(f'norm_ppf needs 0 < p < 1, got {p!r}')
    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        return ((((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) /
                ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0))
    if p > 1.0 - _P_LOW:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -((((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) /
                 ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0))
    q = p - 0.5
    r = q * q
    return ((((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q /
            (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0))


def expected_max(n, sd):
    """The score the best of `n` independent tries reaches on luck alone, at spread `sd`.

    Returns None when the inputs cannot support an answer: one trial has no maximum to
    speak of, and a zero spread means every trial scored alike.
    """
    if n is None or sd is None:
        return None
    n = int(n)
    sd = float(sd)
    if n < 2 or not (sd > 0.0) or not math.isfinite(sd):
        return None
    return sd * ((1.0 - EULER) * norm_ppf(1.0 - 1.0 / n)
                 + EULER * norm_ppf(1.0 - 1.0 / (n * math.e)))


class TrialStats:
    """Count, mean and spread of every score the search has looked at, in three numbers.

    Kept as raw sums rather than a sample: the point is a lifetime figure that survives
    a restart, and a round evaluates thousands of candidates we have no reason to store.
    """

    __slots__ = ('n', 'total', 'sq')

    def __init__(self, n=None, total=None, sq=None):
        self.n, self.total, self.sq = int(n or 0), float(total or 0.0), float(sq or 0.0)

    def add(self, values):
        """Feed every FINITE score of a round. Invalid candidates are not trials: they
        were rejected before scoring, so they never competed for the maximum."""
        for v in values:
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v):
                self.n += 1
                self.total += v
                self.sq += v * v
        return self

    @property
    def mean(self):
        return self.total / self.n if self.n else None

    @property
    def sd(self):
        if self.n < 2:
            return None
        var = (self.sq - self.total * self.total / self.n) / (self.n - 1)
        return math.sqrt(var) if var > 0.0 else None

    def floor(self, n_trials=None):
        """The noise floor over `n_trials` tries (default: the trials measured here)."""
        return expected_max(self.n if n_trials is None else n_trials, self.sd)

    def load(self, d):
        """Adopt persisted sums in place — callers share one module-level accumulator."""
        o = self.from_dict(d)
        self.n, self.total, self.sq = o.n, o.total, o.sq
        return self

    def to_dict(self):
        return {'trial_n': self.n, 'trial_sum': self.total, 'trial_sq': self.sq}

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(d.get('trial_n'), d.get('trial_sum'), d.get('trial_sq'))
