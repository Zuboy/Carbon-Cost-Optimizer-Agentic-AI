"""Decision scoring engine — scores (region × start_time) options by cost × carbon.

Full normalization and argmin implemented in T-09.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

# User objective keyword → (w_cost, w_carbon)
OBJECTIVE_WEIGHTS: dict[str, tuple[float, float]] = {
    "low carbon": (0.2, 0.8),
    "carbon": (0.2, 0.8),
    "green": (0.2, 0.8),
    "cheapest": (0.9, 0.1),
    "cost": (0.9, 0.1),
    "cheap": (0.9, 0.1),
    "balanced": (0.5, 0.5),
}

# Per-instance power draw in kW (TDP-derived estimates)
INSTANCE_POWER_KW: dict[str, float] = {
    "ml.g5.2xlarge": 0.30,
    "ml.g5.12xlarge": 1.20,
    "ml.g5.48xlarge": 4.80,
    "ml.p4d.24xlarge": 6.50,
    "ml.trn1.32xlarge": 5.60,
    "ml.trn1n.32xlarge": 5.60,
    "ml.m5.4xlarge": 0.12,
    "ml.m5.12xlarge": 0.36,
    "ml.c5.4xlarge": 0.09,
    "ml.c5.9xlarge": 0.20,
}
DEFAULT_POWER_KW = 0.5  # fallback for unknown instances


@dataclass
class ScoringOption:
    region: str
    start_time: datetime
    price_usd_per_hr: float
    gco2_kwh: float
    instance_type: str
    instance_count: int = 1


@dataclass
class ScoredOption:
    option: ScoringOption
    cost_usd: float
    carbon_gco2: float
    score: float  # lower = better


def parse_weights(objective: str) -> tuple[float, float]:
    """Parse user objective → (w_cost, w_carbon). Defaults to balanced (0.5, 0.5)."""
    obj_lower = objective.lower()
    for keyword, weights in OBJECTIVE_WEIGHTS.items():
        if keyword in obj_lower:
            return weights
    return (0.5, 0.5)


def score_options(
    options: list[ScoringOption],
    est_runtime_hr: float,
    deadline: datetime,
    objective: str,
) -> list[ScoredOption]:
    """Score feasible options by min-max normalized cost × carbon; return sorted best-first."""
    w_cost, w_carbon = parse_weights(objective)
    deadline_ts = deadline.timestamp()
    results: list[ScoredOption] = []

    for opt in options:
        finish_ts = opt.start_time.timestamp() + est_runtime_hr * 3600
        if finish_ts > deadline_ts:
            continue  # infeasible — misses deadline

        power_kw = INSTANCE_POWER_KW.get(opt.instance_type, DEFAULT_POWER_KW)
        kwh = est_runtime_hr * power_kw * opt.instance_count
        cost = opt.price_usd_per_hr * est_runtime_hr * opt.instance_count
        carbon = opt.gco2_kwh * kwh

        results.append(ScoredOption(option=opt, cost_usd=cost, carbon_gco2=carbon, score=0.0))

    if not results:
        return []

    min_cost = min(r.cost_usd for r in results)
    max_cost = max(r.cost_usd for r in results)
    min_carbon = min(r.carbon_gco2 for r in results)
    max_carbon = max(r.carbon_gco2 for r in results)

    cost_range = max_cost - min_cost
    carbon_range = max_carbon - min_carbon

    for r in results:
        norm_cost = (r.cost_usd - min_cost) / cost_range if cost_range > 0 else 0.0
        norm_carbon = (r.carbon_gco2 - min_carbon) / carbon_range if carbon_range > 0 else 0.0
        r.score = w_cost * norm_cost + w_carbon * norm_carbon

    return sorted(results, key=lambda r: (r.score, r.carbon_gco2))
