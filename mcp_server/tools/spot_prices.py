"""get_spot_prices — EC2 spot price history + on-demand baseline per instance.

Data sources:
  - EC2  describe_spot_price_history(most recent price per AZ, last 1h window)
  - AWS  Pricing API(on-demand baseline, us-east-1 endpoint only)

Results cached for 5 minutes in Lambda container
calls when the agent queries the same region+instance multiple times in one reasoning loop.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
import boto3
from pydantic import BaseModel

REGION_TO_LOCATION: dict[str, str] = {
    "us-east-1":      "US East (N. Virginia)",
    "us-east-2":      "US East (Ohio)",
    "us-west-1":      "US West (N. California)",
    "us-west-2":      "US West (Oregon)",
    "eu-west-1":      "Europe (Ireland)",
    "eu-west-2":      "Europe (London)",
    "eu-central-1":   "Europe (Frankfurt)",
    "eu-north-1":     "Europe (Stockholm)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ca-central-1":   "Canada (Central)",
    "sa-east-1":      "South America (Sao Paulo)",
}

#TTL 
_PRICE_CACHE: dict[str, tuple[float, "SpotPriceResult"]] = {}
_CACHE_TTL_SEC = 300  # 5 minutes

class AZPrice(BaseModel):
    az: str
    usd_per_hr: float
class SpotPriceResult(BaseModel):
    region: str
    instance_type: str
    az_prices: list[AZPrice]
    on_demand_usd_per_hr: float
    retrieved_at: str

#fetch prices function
def _fetch_spot_prices(region: str, instance_type: str) -> list[AZPrice]:
    """Call EC2, return one price per AZ (most recent)."""
    ec2 = boto3.client("ec2", region_name=region)
    start_time = datetime.now(timezone.utc) - timedelta(hours=1)

    paginator = ec2.get_paginator("describe_spot_price_history")
    az_latest: dict[str, float] = {}

    for page in paginator.paginate(
        InstanceTypes=[instance_type],
        ProductDescriptions=["Linux/UNIX"],
        StartTime=start_time,
    ):
        for entry in page["SpotPriceHistory"]:
            az = entry["AvailabilityZone"]
            # API returns newest first per AZ, so order the things in A-Z format ONLY IMP !!
            if az not in az_latest:
                az_latest[az] = float(entry["SpotPrice"])

    return [AZPrice(az=az, usd_per_hr=price) for az, price in sorted(az_latest.items())]


def _fetch_on_demand_price(region:str, instance_type: str)-> float:
    """Query AWS Pricing API for the Linux on-demand price of instance_type in region."""
    location = REGION_TO_LOCATION.get(region)
    if not location:
        return 0.0  # unknown region — no Pricing API call

    # Pricing API is only available in us-east-1 (global endpoint) for now 
    pricing = boto3.client("pricing",region_name="us-east-1")
    resp = pricing.get_products(
        ServiceCode="AmazonEC2",
        Filters=[
            {"Type": "TERM_MATCH", "Field": "instanceType",    "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
            {"Type": "TERM_MATCH", "Field": "location",        "Value": location},
            {"Type": "TERM_MATCH", "Field": "tenancy",         "Value": "Shared"},
            {"Type": "TERM_MATCH", "Field": "capacitystatus",  "Value": "Used"},
            {"Type": "TERM_MATCH", "Field": "preInstalledSw",  "Value": "NA"},
        ],
        MaxResults=10,
    )

    for price_str in resp.get("PriceList",[]):
        price_data = json.loads(price_str)
        for term in price_data.get("terms",{}).get("OnDemand",{}).values():
            for dim in term.get("priceDimensions",{}).values():
                usd = float(dim.get("pricePerUnit",{}).get("USD","0"))
                if usd > 0:
                    return usd

    return 0.0  # no price found (e.g. instance not available in that region)


# ── MCP tool ──────────────────────────────────────────────────────────────────

async def get_spot_prices(region: str, instance_type: str) -> SpotPriceResult:
    """Return current EC2 spot prices and on-demand baseline for a region and instance type.

    Results are cached for 5 minutes. Returns an empty az_prices list if no spot
    market exists for the instance in that region.
    """
    cache_key = f"{region}:{instance_type}"
    entry = _PRICE_CACHE.get(cache_key)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL_SEC:
        return entry[1]

    az_prices = _fetch_spot_prices(region, instance_type)
    on_demand = _fetch_on_demand_price(region, instance_type)

    result = SpotPriceResult(
        region=region,
        instance_type=instance_type,
        az_prices=az_prices,
        on_demand_usd_per_hr=on_demand,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
    )
    _PRICE_CACHE[cache_key] = (time.monotonic(), result)
    return result
