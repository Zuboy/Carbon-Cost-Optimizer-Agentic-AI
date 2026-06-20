"""get_spot_prices — EC2 spot price history + on-demand baseline per region/instance.

Real implementation added in T-05. Returns mock data until then.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pydantic import BaseModel


class AZPrice(BaseModel):
    az: str
    usd_per_hr: float


class SpotPriceResult(BaseModel):
    region: str
    instance_type: str
    az_prices: list[AZPrice]
    on_demand_usd_per_hr: float
    retrieved_at: str


async def get_spot_prices(region: str, instance_type: str) -> SpotPriceResult:
    """Return current EC2 spot prices and on-demand baseline for a region and instance type."""
    # TODO (T-05): replace with ec2.describe_spot_price_history + pricing.get_products
    return SpotPriceResult(
        region=region,
        instance_type=instance_type,
        az_prices=[AZPrice(az=f"{region}a", usd_per_hr=0.50)],
        on_demand_usd_per_hr=2.00,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
    )
