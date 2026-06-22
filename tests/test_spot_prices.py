"""Unit tests for get_spot_prices — all boto3 calls are mocked; no real AWS."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import mcp_server.tools.spot_prices as module
from mcp_server.tools.spot_prices import SpotPriceResult, get_spot_prices


# ── Shared mock data ──────────────────────────────────────────────────────────

_NOW = datetime.now(timezone.utc)

SPOT_PAGE = {
    "SpotPriceHistory": [
        {"AvailabilityZone": "us-west-2a", "SpotPrice": "0.45", "Timestamp": _NOW},
        {"AvailabilityZone": "us-west-2b", "SpotPrice": "0.50", "Timestamp": _NOW},
        {"AvailabilityZone": "us-west-2c", "SpotPrice": "0.42", "Timestamp": _NOW},
        # Duplicate AZ — should keep first (most recent) price only
        {"AvailabilityZone": "us-west-2a", "SpotPrice": "0.99", "Timestamp": _NOW},
    ]
}

PRICING_RESPONSE = {
    "PriceList": [
        json.dumps({
            "terms": {
                "OnDemand": {
                    "term1": {
                        "priceDimensions": {
                            "dim1": {"pricePerUnit": {"USD": "2.00"}}
                        }
                    }
                }
            }
        })
    ]
}


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the module-level price cache before every test."""
    module._PRICE_CACHE.clear()
    yield
    module._PRICE_CACHE.clear()


@pytest.fixture
def mock_boto3():
    ec2 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = iter([SPOT_PAGE])
    ec2.get_paginator.return_value = paginator

    pricing = MagicMock()
    pricing.get_products.return_value = PRICING_RESPONSE

    def _client(service, **kwargs):
        return ec2 if service == "ec2" else pricing

    with patch.object(module, "boto3") as mock:
        mock.client.side_effect = _client
        yield {"ec2": ec2, "pricing": pricing}


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_spot_price_result(mock_boto3):
    result = await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    assert isinstance(result, SpotPriceResult)


@pytest.mark.asyncio
async def test_region_and_instance_echoed(mock_boto3):
    result = await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    assert result.region == "us-west-2"
    assert result.instance_type == "ml.g5.2xlarge"


@pytest.mark.asyncio
async def test_three_az_prices_returned(mock_boto3):
    result = await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    # Duplicate AZ should be de-duped → 3 unique AZs
    assert len(result.az_prices) == 3


@pytest.mark.asyncio
async def test_az_prices_are_sorted(mock_boto3):
    result = await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    azs = [p.az for p in result.az_prices]
    assert azs == sorted(azs)


@pytest.mark.asyncio
async def test_duplicate_az_keeps_first_price(mock_boto3):
    result = await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    price_2a = next(p.usd_per_hr for p in result.az_prices if p.az == "us-west-2a")
    assert price_2a == 0.45  # first entry, not the duplicate 0.99


@pytest.mark.asyncio
async def test_on_demand_price(mock_boto3):
    result = await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    assert result.on_demand_usd_per_hr == 2.00


@pytest.mark.asyncio
async def test_retrieved_at_is_iso8601(mock_boto3):
    result = await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    datetime.fromisoformat(result.retrieved_at)  # raises if malformed


# ── Cache behaviour ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_second_call_uses_cache(mock_boto3):
    await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    # EC2 paginator should only be called once
    assert mock_boto3["ec2"].get_paginator.call_count == 1


@pytest.mark.asyncio
async def test_different_instance_types_not_shared_cache(mock_boto3):
    await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    await get_spot_prices("us-west-2", "ml.p4d.24xlarge")
    assert mock_boto3["ec2"].get_paginator.call_count == 2


# ── Edge cases ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_spot_market_returns_empty_list(mock_boto3):
    mock_boto3["ec2"].get_paginator.return_value.paginate.return_value = iter(
        [{"SpotPriceHistory": []}]
    )
    result = await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    assert result.az_prices == []


@pytest.mark.asyncio
async def test_unknown_region_skips_pricing_api(mock_boto3):
    result = await get_spot_prices("ap-unknown-99", "ml.g5.2xlarge")
    assert result.on_demand_usd_per_hr == 0.0
    # Pricing client should NOT have been called
    mock_boto3["pricing"].get_products.assert_not_called()


@pytest.mark.asyncio
async def test_no_pricing_result_returns_zero(mock_boto3):
    mock_boto3["pricing"].get_products.return_value = {"PriceList": []}
    result = await get_spot_prices("us-west-2", "ml.g5.2xlarge")
    assert result.on_demand_usd_per_hr == 0.0


@pytest.mark.asyncio
async def test_pricing_api_called_in_us_east_1(mock_boto3):
    await get_spot_prices("eu-north-1", "ml.g5.2xlarge")
    calls = mock_boto3["pricing"].get_products.call_args_list
    assert len(calls) == 1
    # Verify Pricing API was pointed at us-east-1 (checked via boto3.client call args)
    client_calls = [
        call for call in module.boto3.client.call_args_list
        if call.args and call.args[0] == "pricing"
    ]
    assert any(c.kwargs.get("region_name") == "us-east-1" for c in client_calls)
