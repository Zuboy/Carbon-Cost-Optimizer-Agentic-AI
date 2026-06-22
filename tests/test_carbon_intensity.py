"""Unit tests for get_carbon_intensity — all HTTP calls mocked with respx."""
from __future__ import annotations

from unittest.mock import patch

import pytest
import respx
from httpx import Response

import mcp_server.tools.carbon_intensity as module
from mcp_server.tools.carbon_intensity import (
    ELECMAPS_BASE,
    WATTTIME_BASE,
    CarbonIntensityResult,
    get_carbon_intensity,
)

# ── Shared mock payloads ──────────────────────────────────────────────────────

WT_TOKEN_RESP    = {"token": "fake-wt-token"}
WT_RT_MOER_RESP  = {"value": 220.0, "ba": "CAISO_NORTH", "signal_type": "co2_moer"}
WT_RT_INDEX_RESP = {"percent": 45.2, "ba": "CAISO_NORTH", "signal_type": "co2_moer"}
WT_FORECAST_RESP = {
    "forecast": [
        {"point_time": "2026-06-22T10:00:00Z", "value": 200.0},
        {"point_time": "2026-06-22T11:00:00Z", "value": 180.0},
    ]
}

EM_RT_RESP = {"carbonIntensity": 150.0, "datetime": "2026-06-22T09:00:00Z"}
EM_FORECAST_RESP = {
    "forecast": [
        {"datetime": "2026-06-22T10:00:00Z", "carbonIntensity": 140.0},
        {"datetime": "2026-06-22T11:00:00Z", "carbonIntensity": 130.0},
    ]
}

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_module_state():
    """Clear token + result caches and patch config helpers before each test."""
    module._WT_TOKEN.clear()
    module._CARBON_CACHE.clear()
    with (
        patch.object(module, "get_watttime_credentials",
                     return_value={"username": "u", "password": "p"}),
        patch.object(module, "get_electricity_maps_key",
                     return_value="fake-em-key"),
    ):
        yield
    module._WT_TOKEN.clear()
    module._CARBON_CACHE.clear()


# ── WattTime Pro path (returns `value` in lbs CO₂/MWh) ──────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_watttime_pro_returns_marginal():
    respx.post(f"{WATTTIME_BASE}/login").mock(return_value=Response(200, json=WT_TOKEN_RESP))
    respx.get(f"{WATTTIME_BASE}/signal-index").mock(return_value=Response(200, json=WT_RT_MOER_RESP))
    respx.get(f"{WATTTIME_BASE}/forecast").mock(return_value=Response(200, json=WT_FORECAST_RESP))

    result = await get_carbon_intensity("us-west-2")

    assert isinstance(result, CarbonIntensityResult)
    assert result.signal_type == "marginal"
    assert result.region == "us-west-2"
    assert result.zone == "CAISO_NORTH"


@respx.mock
@pytest.mark.asyncio
async def test_watttime_converts_lbs_to_gco2(monkeypatch):
    respx.post(f"{WATTTIME_BASE}/login").mock(return_value=Response(200, json=WT_TOKEN_RESP))
    respx.get(f"{WATTTIME_BASE}/signal-index").mock(return_value=Response(200, json=WT_RT_MOER_RESP))
    respx.get(f"{WATTTIME_BASE}/forecast").mock(return_value=Response(200, json=WT_FORECAST_RESP))

    result = await get_carbon_intensity("us-west-2")

    # 220.0 lbs/MWh × 0.453592 = 99.79 gCO₂/kWh
    assert abs(result.current_gco2_kwh - 220.0 * 0.453592) < 0.01


@respx.mock
@pytest.mark.asyncio
async def test_watttime_forecast_converted():
    respx.post(f"{WATTTIME_BASE}/login").mock(return_value=Response(200, json=WT_TOKEN_RESP))
    respx.get(f"{WATTTIME_BASE}/signal-index").mock(return_value=Response(200, json=WT_RT_MOER_RESP))
    respx.get(f"{WATTTIME_BASE}/forecast").mock(return_value=Response(200, json=WT_FORECAST_RESP))

    result = await get_carbon_intensity("us-west-2")

    assert len(result.forecast) == 2
    assert abs(result.forecast[0].gco2_kwh - 200.0 * 0.453592) < 0.01


@respx.mock
@pytest.mark.asyncio
async def test_watttime_token_cached():
    login_route = respx.post(f"{WATTTIME_BASE}/login").mock(
        return_value=Response(200, json=WT_TOKEN_RESP)
    )
    respx.get(f"{WATTTIME_BASE}/signal-index").mock(return_value=Response(200, json=WT_RT_MOER_RESP))
    respx.get(f"{WATTTIME_BASE}/forecast").mock(return_value=Response(200, json=WT_FORECAST_RESP))

    await get_carbon_intensity("us-west-2")
    module._CARBON_CACHE.clear()  # clear result cache but keep token
    await get_carbon_intensity("us-west-2")

    # Login should only be called once despite two get_carbon_intensity calls
    assert login_route.call_count == 1


# ── WattTime free-tier fallthrough ───────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_watttime_free_tier_falls_back_to_elecmaps():
    """When WattTime returns `percent` (free tier index), we fall back to Electricity Maps."""
    respx.post(f"{WATTTIME_BASE}/login").mock(return_value=Response(200, json=WT_TOKEN_RESP))
    # Free tier: returns percent, not value
    respx.get(f"{WATTTIME_BASE}/signal-index").mock(
        return_value=Response(200, json=WT_RT_INDEX_RESP)
    )
    respx.get(f"{ELECMAPS_BASE}/carbon-intensity/latest").mock(
        return_value=Response(200, json=EM_RT_RESP)
    )
    respx.get(f"{ELECMAPS_BASE}/carbon-intensity/forecast").mock(
        return_value=Response(200, json=EM_FORECAST_RESP)
    )

    result = await get_carbon_intensity("us-west-2")

    assert result.signal_type == "average"
    assert result.current_gco2_kwh == 150.0


# ── Electricity Maps path ─────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_electricity_maps_used_when_watttime_down():
    """WattTime 500 → falls back to Electricity Maps."""
    respx.post(f"{WATTTIME_BASE}/login").mock(return_value=Response(500))
    respx.get(f"{ELECMAPS_BASE}/carbon-intensity/latest").mock(
        return_value=Response(200, json=EM_RT_RESP)
    )
    respx.get(f"{ELECMAPS_BASE}/carbon-intensity/forecast").mock(
        return_value=Response(200, json=EM_FORECAST_RESP)
    )

    result = await get_carbon_intensity("us-west-2")

    assert result.signal_type == "average"
    assert result.current_gco2_kwh == 150.0
    assert result.zone == "US-CAL-CISO"


@respx.mock
@pytest.mark.asyncio
async def test_electricity_maps_forecast_parsed():
    respx.post(f"{WATTTIME_BASE}/login").mock(return_value=Response(500))
    respx.get(f"{ELECMAPS_BASE}/carbon-intensity/latest").mock(
        return_value=Response(200, json=EM_RT_RESP)
    )
    respx.get(f"{ELECMAPS_BASE}/carbon-intensity/forecast").mock(
        return_value=Response(200, json=EM_FORECAST_RESP)
    )

    result = await get_carbon_intensity("us-west-2")

    assert len(result.forecast) == 2
    assert result.forecast[0].gco2_kwh == 140.0
    assert result.forecast[1].gco2_kwh == 130.0


@respx.mock
@pytest.mark.asyncio
async def test_electricity_maps_no_forecast_returns_empty_list():
    respx.post(f"{WATTTIME_BASE}/login").mock(return_value=Response(500))
    respx.get(f"{ELECMAPS_BASE}/carbon-intensity/latest").mock(
        return_value=Response(200, json=EM_RT_RESP)
    )
    respx.get(f"{ELECMAPS_BASE}/carbon-intensity/forecast").mock(
        return_value=Response(500)
    )

    result = await get_carbon_intensity("us-west-2")

    assert result.forecast == []


# ── Cache behaviour ───────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_result_cached_on_second_call():
    respx.post(f"{WATTTIME_BASE}/login").mock(return_value=Response(500))
    rt_route = respx.get(f"{ELECMAPS_BASE}/carbon-intensity/latest").mock(
        return_value=Response(200, json=EM_RT_RESP)
    )
    respx.get(f"{ELECMAPS_BASE}/carbon-intensity/forecast").mock(
        return_value=Response(200, json=EM_FORECAST_RESP)
    )

    await get_carbon_intensity("us-west-2")
    await get_carbon_intensity("us-west-2")

    assert rt_route.call_count == 1  # second call served from cache


# ── Error cases ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_region_raises_value_error():
    with pytest.raises(ValueError, match="Unsupported region"):
        await get_carbon_intensity("ap-unknown-99")


@respx.mock
@pytest.mark.asyncio
async def test_both_apis_down_raises_runtime_error():
    respx.post(f"{WATTTIME_BASE}/login").mock(return_value=Response(500))
    respx.get(f"{ELECMAPS_BASE}/carbon-intensity/latest").mock(return_value=Response(500))

    with pytest.raises(RuntimeError, match="No carbon intensity data available"):
        await get_carbon_intensity("us-west-2")
