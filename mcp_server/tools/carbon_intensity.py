"""get_carbon_intensity — grid carbon intensity (gCO2/kWh) + 24h forecast per AWS region.

Real implementation added in T-06. Returns mock data until then.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pydantic import BaseModel

# AWS region → grid balancing authority zone
REGION_TO_ZONE: dict[str, str] = {
    "us-west-2": "CAISO_NORTH",   # WattTime free tier covers this zone
    "eu-north-1": "SE",
    "ca-central-1": "AESO",
    "us-east-1": "PJM",
    "eu-west-1": "IE",
}


class ForecastPoint(BaseModel):
    ts: str
    gco2_kwh: float


class CarbonIntensityResult(BaseModel):
    region: str
    zone: str
    current_gco2_kwh: float
    forecast: list[ForecastPoint]
    signal_type: str  # "marginal" | "average"


async def get_carbon_intensity(region: str) -> CarbonIntensityResult:
    """Return current carbon intensity and 24h forecast for an AWS region."""
    zone = REGION_TO_ZONE.get(region, "UNKNOWN")
    # TODO (T-06): replace with WattTime /v3/signal-index + Electricity Maps fallback
    return CarbonIntensityResult(
        region=region,
        zone=zone,
        current_gco2_kwh=150.0,
        forecast=[ForecastPoint(ts=datetime.now(timezone.utc).isoformat(), gco2_kwh=150.0)],
        signal_type="average",
    )
