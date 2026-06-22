"""Tests for agent/scoring.py — decision scoring engine."""
from __future__ import annotations
import pytest
from datetime import datetime, timezone, timedelta

from agent.scoring import (
    ScoringOption,
    ScoredOption,
    parse_weights,
    score_options,
    OBJECTIVE_WEIGHTS,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
DEADLINE = NOW + timedelta(hours=12)


def _opt(
    region: str = "us-west-2",
    start_offset_hr: float = 0.0,
    price: float = 1.0,
    gco2: float = 100.0,
    instance_type: str = "ml.g5.2xlarge",
    count: int = 1,
) -> ScoringOption:
    return ScoringOption(
        region=region,
        start_time=NOW + timedelta(hours=start_offset_hr),
        price_usd_per_hr=price,
        gco2_kwh=gco2,
        instance_type=instance_type,
        instance_count=count,
    )


# ---------------------------------------------------------------------------
# parse_weights
# ---------------------------------------------------------------------------

class TestParseWeights:
    def test_low_carbon(self):
        w_cost, w_carbon = parse_weights("low carbon")
        assert w_carbon == 0.8 and w_cost == 0.2

    def test_cheapest(self):
        w_cost, w_carbon = parse_weights("cheapest run please")
        assert w_cost == 0.9 and w_carbon == 0.1

    def test_green_keyword(self):
        assert parse_weights("green option") == (0.2, 0.8)

    def test_balanced(self):
        assert parse_weights("balanced") == (0.5, 0.5)

    def test_unknown_defaults_balanced(self):
        assert parse_weights("fastest possible") == (0.5, 0.5)

    def test_case_insensitive(self):
        assert parse_weights("LOW CARBON") == (0.2, 0.8)

    @pytest.mark.parametrize("keyword", list(OBJECTIVE_WEIGHTS.keys()))
    def test_all_keywords_resolve(self, keyword):
        w_cost, w_carbon = parse_weights(keyword)
        assert w_cost + w_carbon == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# score_options — deadline filtering
# ---------------------------------------------------------------------------

class TestDeadlineFiltering:
    def test_infeasible_option_excluded(self):
        # runtime=6h, starts 8h before deadline → finishes 14h after NOW, past 12h deadline
        late_opt = _opt(start_offset_hr=8.0)
        results = score_options([late_opt], est_runtime_hr=6.0, deadline=DEADLINE, objective="balanced")
        assert results == []

    def test_feasible_option_included(self):
        # runtime=2h, starts NOW → finishes at NOW+2h, well within 12h deadline
        opt = _opt()
        results = score_options([opt], est_runtime_hr=2.0, deadline=DEADLINE, objective="balanced")
        assert len(results) == 1

    def test_mixed_feasibility(self):
        good = _opt(start_offset_hr=0.0)
        bad = _opt(start_offset_hr=10.0)  # 10+2 = 12h, exceeds deadline
        results = score_options([good, bad], est_runtime_hr=3.0, deadline=DEADLINE, objective="balanced")
        assert len(results) == 1
        assert results[0].option.region == good.region


# ---------------------------------------------------------------------------
# score_options — empty input
# ---------------------------------------------------------------------------

class TestEmptyInput:
    def test_no_options_returns_empty(self):
        assert score_options([], est_runtime_hr=1.0, deadline=DEADLINE, objective="balanced") == []

    def test_all_infeasible_returns_empty(self):
        opts = [_opt(start_offset_hr=11.0), _opt(start_offset_hr=11.5)]
        assert score_options(opts, est_runtime_hr=2.0, deadline=DEADLINE, objective="balanced") == []


# ---------------------------------------------------------------------------
# score_options — single option
# ---------------------------------------------------------------------------

class TestSingleOption:
    def test_single_option_score_is_zero(self):
        """With one option, both normalized dimensions are 0 → score must be 0."""
        results = score_options([_opt()], est_runtime_hr=1.0, deadline=DEADLINE, objective="balanced")
        assert len(results) == 1
        assert results[0].score == pytest.approx(0.0)

    def test_single_option_cost_and_carbon_computed(self):
        # ml.g5.2xlarge: 0.30 kW, 1h runtime, $2/hr, 200 gCO2/kWh
        opt = _opt(price=2.0, gco2=200.0, instance_type="ml.g5.2xlarge", count=1)
        result = score_options([opt], est_runtime_hr=1.0, deadline=DEADLINE, objective="balanced")[0]
        assert result.cost_usd == pytest.approx(2.0)          # $2/hr × 1hr × 1
        assert result.carbon_gco2 == pytest.approx(60.0)      # 200 gCO2/kWh × 0.30kW × 1h


# ---------------------------------------------------------------------------
# score_options — normalization correctness
# ---------------------------------------------------------------------------

class TestMinMaxNormalization:
    def test_cheapest_wins_cost_objective(self):
        cheap = _opt(region="us-west-2", price=0.5, gco2=200.0)
        expensive = _opt(region="eu-north-1", price=2.0, gco2=100.0)
        results = score_options([cheap, expensive], est_runtime_hr=1.0, deadline=DEADLINE, objective="cheapest")
        assert results[0].option.region == "us-west-2"

    def test_greenest_wins_carbon_objective(self):
        dirty = _opt(region="us-west-2", price=0.5, gco2=500.0)
        clean = _opt(region="eu-north-1", price=2.0, gco2=50.0)
        results = score_options([dirty, clean], est_runtime_hr=1.0, deadline=DEADLINE, objective="low carbon")
        assert results[0].option.region == "eu-north-1"

    def test_all_equal_cost_scores_zero_on_cost_dim(self):
        """When all options have identical cost, cost dimension contributes 0 to every score."""
        opts = [_opt(region=r, price=1.0, gco2=float(100 + i * 50)) for i, r in enumerate(["us-west-2", "eu-north-1", "ca-central-1"])]
        results = score_options(opts, est_runtime_hr=1.0, deadline=DEADLINE, objective="cheapest")
        # With w_cost=0.9, w_carbon=0.1 and cost all equal → score = 0.1 * norm_carbon
        # Best = lowest carbon = us-west-2 (100 gCO2/kWh)
        assert results[0].option.region == "us-west-2"

    def test_scores_bounded_zero_to_one(self):
        opts = [
            _opt(region="us-west-2", price=0.1, gco2=10.0),
            _opt(region="eu-north-1", price=5.0, gco2=500.0),
            _opt(region="ca-central-1", price=2.5, gco2=250.0),
        ]
        results = score_options(opts, est_runtime_hr=1.0, deadline=DEADLINE, objective="balanced")
        for r in results:
            assert 0.0 <= r.score <= 1.0

    def test_best_option_has_lowest_score(self):
        opts = [
            _opt(region="us-west-2", price=0.1, gco2=10.0),   # clearly best
            _opt(region="eu-north-1", price=5.0, gco2=500.0),  # clearly worst
        ]
        results = score_options(opts, est_runtime_hr=1.0, deadline=DEADLINE, objective="balanced")
        assert results[0].score < results[-1].score

    def test_worst_option_has_score_one(self):
        """The dominated option gets norm_cost=1, norm_carbon=1 → score = w_cost + w_carbon = 1."""
        opts = [
            _opt(region="us-west-2", price=0.1, gco2=10.0),
            _opt(region="eu-north-1", price=5.0, gco2=500.0),
        ]
        results = score_options(opts, est_runtime_hr=1.0, deadline=DEADLINE, objective="balanced")
        worst = results[-1]
        assert worst.score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# score_options — tie-breaking
# ---------------------------------------------------------------------------

class TestTieBreaking:
    def test_tie_broken_by_carbon(self):
        """Two options with identical composite score → lower carbon wins."""
        # With balanced weights and symmetric differences, craft a tie
        # opt_a: high cost, low carbon; opt_b: low cost, high carbon → equal balanced score
        opt_a = _opt(region="us-west-2", price=2.0, gco2=100.0)
        opt_b = _opt(region="eu-north-1", price=1.0, gco2=200.0)
        results = score_options([opt_a, opt_b], est_runtime_hr=1.0, deadline=DEADLINE, objective="balanced")
        # Both get score 0.5; tie-break: lower carbon_gco2 first
        assert results[0].option.region == "us-west-2"


# ---------------------------------------------------------------------------
# score_options — instance count and unknown instances
# ---------------------------------------------------------------------------

class TestInstanceDetails:
    def test_multi_instance_scales_cost_and_carbon(self):
        opt = _opt(price=1.0, gco2=100.0, instance_type="ml.g5.2xlarge", count=4)
        result = score_options([opt], est_runtime_hr=1.0, deadline=DEADLINE, objective="balanced")[0]
        # cost = $1/hr × 1h × 4 = $4
        assert result.cost_usd == pytest.approx(4.0)
        # carbon = 100 gCO2/kWh × 0.30 kW × 1h × 4 = 120 gCO2
        assert result.carbon_gco2 == pytest.approx(120.0)

    def test_unknown_instance_uses_default_power(self):
        opt = _opt(price=1.0, gco2=100.0, instance_type="ml.unknown.huge", count=1)
        result = score_options([opt], est_runtime_hr=2.0, deadline=DEADLINE, objective="balanced")[0]
        # DEFAULT_POWER_KW = 0.5; carbon = 100 × 0.5 × 2 = 100
        assert result.carbon_gco2 == pytest.approx(100.0)
