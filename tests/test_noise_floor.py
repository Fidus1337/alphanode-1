"""The noise floor: what the best of N tries scores when nothing in the data is real.

The claim the feature makes to the user is falsifiable, so these tests falsify it: draw
N scores from pure noise, take the maximum, and check the formula predicted it.
"""
import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'evolution'))

from deflate import EULER, TrialStats, expected_max, norm_ppf   # noqa: E402


# ---------------- the inverse normal CDF ----------------
@pytest.mark.parametrize('p, want', [
    (0.5, 0.0),
    (0.975, 1.959964),
    (0.99, 2.326348),
    (0.025, -1.959964),
    (0.999975, 4.055627),          # the tail the floor actually lives in (N = 40,000)
])
def test_norm_ppf_matches_the_table(p, want):
    assert norm_ppf(p) == pytest.approx(want, abs=2e-6)


def test_norm_ppf_matches_scipy_across_the_range():
    """scipy is the oracle, not the implementation: the node ships without it, because a
    frozen build must not gain a numerical stack for one inverse CDF."""
    scipy_stats = pytest.importorskip('scipy.stats')
    for p in (1e-6, 0.001, 0.02, 0.2, 0.5, 0.8, 0.98, 0.999, 1 - 1e-6, 1 - 1e-7):
        assert norm_ppf(p) == pytest.approx(scipy_stats.norm.ppf(p), abs=1e-6)


def test_norm_ppf_rejects_impossible_probabilities():
    for p in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            norm_ppf(p)


# ---------------- the floor itself ----------------
def test_floor_predicts_the_maximum_of_pure_noise():
    """The whole point, checked against a simulation rather than against itself."""
    rng = random.Random(20260827)
    n, sd, reps = 2000, 0.4, 400
    observed = sum(max(rng.gauss(0.0, sd) for _ in range(n)) for _ in range(reps)) / reps
    predicted = expected_max(n, sd)
    assert predicted == pytest.approx(observed, rel=0.02)


def test_floor_scales_with_the_spread_and_creeps_with_the_count():
    # doubling the spread doubles the bar — it is a pure multiple of sd
    assert expected_max(10_000, 0.8) == pytest.approx(2 * expected_max(10_000, 0.4))
    # …while 25x the trials moves it by well under a third: mining longer raises the bar,
    # but slowly, which is the property that makes a long run's champion suspicious
    small, large = expected_max(1_600, 0.4), expected_max(40_000, 0.4)
    assert large > small
    assert large / small < 1.3


def test_floor_is_the_headline_arithmetic():
    """40,000 tries at a spread of 0.4 puts the bar near +1.7 — the number quoted to the
    user. If this drifts, the explanation in the manual has stopped being true."""
    assert expected_max(40_000, 0.4) == pytest.approx(1.68, abs=0.03)


@pytest.mark.parametrize('n, sd', [(1, 0.4), (0, 0.4), (None, 0.4), (10, 0.0),
                                   (10, -1.0), (10, None), (10, float('nan'))])
def test_floor_declines_to_answer_without_evidence(n, sd):
    assert expected_max(n, sd) is None


def test_euler_constant_is_the_one_the_formula_wants():
    assert EULER == pytest.approx(0.5772156649, abs=1e-9)


# ---------------- the lifetime accumulator ----------------
def test_stats_measure_what_they_were_given():
    vals = [0.1, -0.4, 1.2, 0.7, -1.1]
    st = TrialStats().add(vals)
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    assert st.n == 5
    assert st.mean == pytest.approx(mean)
    assert st.sd == pytest.approx(math.sqrt(var))


def test_stats_ignore_what_never_competed():
    """A candidate rejected before scoring is not a trial — it never had a chance at the
    maximum, so counting it would deflate the spread and lower the bar for free."""
    st = TrialStats().add([1.0, float('nan'), float('inf'), None, 'x', -1.0])
    assert st.n == 2
    assert st.mean == pytest.approx(0.0)


def test_stats_accumulate_across_rounds():
    a = TrialStats().add([0.1, 0.5, -0.3])
    a.add([1.4, -0.8])
    b = TrialStats().add([0.1, 0.5, -0.3, 1.4, -0.8])
    assert a.n == b.n
    assert a.sd == pytest.approx(b.sd)


def test_stats_survive_a_restart():
    """The floor is a lifetime figure: a restart must not silently reset the bar to zero."""
    a = TrialStats().add([0.2, -0.6, 1.1, 0.4])
    revived = TrialStats.from_dict(a.to_dict())
    assert (revived.n, revived.mean, revived.sd) == (a.n, a.mean, a.sd)
    # …and the node adopts them in place, because it shares one module-level accumulator
    live = TrialStats()
    assert live.load(a.to_dict()) is live
    assert live.sd == pytest.approx(a.sd)


def test_stats_read_a_status_dict_with_other_keys_in_it():
    """from_dict is fed the node's whole status.json, not a tailored payload."""
    st = TrialStats.from_dict({'rounds': 12, 'trials_total': 40_000, 'best_base': 1.9,
                               'trial_n': 3, 'trial_sum': 1.5, 'trial_sq': 2.25})
    assert st.n == 3 and st.mean == pytest.approx(0.5)


def test_a_fresh_or_missing_history_has_no_floor():
    assert TrialStats.from_dict(None).floor() is None
    assert TrialStats().floor() is None
    assert TrialStats().add([0.5]).floor() is None          # one trial has no maximum


def test_floor_can_be_asked_about_a_different_trial_count():
    """The measured spread, applied to the node's own lifetime trial counter."""
    st = TrialStats().add([0.1 * i for i in range(-20, 21)])
    assert st.floor(10_000) > st.floor(100) > 0


# ---------------- the product claim ----------------
def test_a_search_over_noise_produces_a_champion_at_the_floor():
    """End to end, in the shape the node uses it: score a population of formulas that have
    no edge, keep the best as a champion, and check the floor catches it.

    One search is a single draw from a Gumbel and scatters, so this runs 25 of them: the
    claim is not that a champion equals the floor, it is that a champion NEVER lands the
    multiples above it that a real edge would."""
    ratios, champions = [], []
    for seed in range(25):
        rng = random.Random(seed)
        scores = [rng.gauss(0.0, 0.45) for _ in range(6_000)]
        floor = TrialStats().add(scores).floor()
        assert floor is not None
        champions.append(max(scores))
        ratios.append(max(scores) / floor)
    # every one of these champions would read as a fine alpha on the leaderboard…
    assert min(champions) > 1.2
    # …and not one of them clears the bar by the margin a real edge would
    assert max(ratios) < 1.35
    assert sum(ratios) / len(ratios) == pytest.approx(1.0, abs=0.06)


# ---------------- the node's side of the wire ----------------
def test_node_writes_the_keys_it_reads_back(monkeypatch):
    """The restart path: what the node puts in status.json must be what it parses out of
    it next launch, or the floor silently resets to zero on every restart."""
    import node as nd
    stats = TrialStats().add([0.3, -0.9, 1.4, 0.2])
    status_like = {'rounds': 3, 'trials_total': 12_000, **stats.to_dict()}
    revived = TrialStats.from_dict(status_like)
    assert (revived.n, revived.sd) == (stats.n, stats.sd)
    assert isinstance(nd.trials, TrialStats)             # the node owns one accumulator


def test_the_gaussian_bar_overshoots_a_thin_tailed_search():
    """Why nothing draws this number yet, kept as a test so the reason cannot be forgotten.

    Measured on a real round: 1,197 scored candidates, mean -0.64, sd 0.95, skew +0.14 and
    excess kurtosis -0.71 — near-symmetric but THIN-tailed, with a hard ceiling near +1.6.
    Every trial is a function of the same finite price history, so the trials are neither
    independent nor unbounded, and extrapolating a Gaussian tail to the 1-in-N quantile
    reaches for a tail the data does not have: the estimate came out at +4.5 on a run whose
    actual best over 9,900 trials was +1.86. A uniform draw reproduces the same failure.
    """
    rng = random.Random(11)
    scores = [rng.uniform(-1.0, 1.0) for _ in range(1_200)]      # thin-tailed and bounded
    stats = TrialStats().add(scores)
    assert stats.floor() > max(scores) * 1.7        # not slightly off — the wrong shape
    # …while the same estimator is right when the trials really are Gaussian, which is
    # what test_floor_predicts_the_maximum_of_pure_noise checks. The maths is sound; the
    # assumption it needs is what a formula search over one price history does not supply.
