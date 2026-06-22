"""get_carbon_intensity — grid carbon intensity (gCO₂/kWh) + 24h forecast per AWS region.

Signal priority:
  1. WattTime  — marginal MOER (lbs CO₂/MWh → gCO₂/kWh). Pro tier gives actual values;
                  free tier returns a 0-100 index which we cannot use as gCO₂/kWh, so we
                  fall through to Electricity Maps in that case.
  2. Electricity Maps — average carbon intensity (gCO₂/kWh). Free tier is non-commercial.

Results are cached for 10 minutes (carbon intensity changes slowly relative to spot prices).

Free-tier caveat (from ARCHITECTURE.md):
  WattTime free → CAISO_NORTH only, index signal only (not gCO₂/kWh).
  Electricity Maps free → average (not marginal), non-commercial use only.
  For a paid PoC, both APIs return real gCO₂/kWh values.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Literal

import httpx
from pydantic import BaseModel

from mcp_server.config import get_electricity_maps_key, get_watttime_credentials

# ── Zone mappings ─────────────────────────────────────────────────────────────

# AWS region → WattTime balancing authority zone
REGION_TO_WT_ZONE: dict[str, str] = {
    "us-west-2":   "CAISO_NORTH",
    "us-east-1":   "PJM",
    "eu-north-1":  "SE",
    "eu-west-1":   "IE",
    "ca-central-1": "AESO",
}

# AWS region → Electricity Maps zone code (different naming convention from WattTime)
REGION_TO_EM_ZONE: dict[str, str] = {
    "us-west-2":   "US-CAL-CISO",
    "us-east-1":   "US-MIDA-PJM",
    "eu-north-1":  "SE",
    "eu-west-1":   "IE",
    "ca-central-1": "CA-AB",
}

WATTTIME_BASE = "https://api.watttime.org/v3"
ELECMAPS_BASE = "https://api.electricitymap.org/v3"

# lbs CO₂/MWh → gCO₂/kWh
_LBS_MWH_TO_GCO2_KWH = 0.453592

# ── Response models ───────────────────────────────────────────────────────────

class ForecastPoint(BaseModel):
    ts: str
    gco2_kwh: float


class CarbonIntensityResult(BaseModel):
    region: str
    zone: str
    current_gco2_kwh: float
    forecast: list[ForecastPoint]
    signal_type: Literal["marginal", "average"]


# ── Token + result caches ─────────────────────────────────────────────────────

_WT_TOKEN: dict[str, str | float] = {}   # {"token": str, "expires": float}
_CARBON_CACHE: dict[str, tuple[float, CarbonIntensityResult]] = {}
_CACHE_TTL_SEC = 600  # 10 minutes


# ── WattTime helpers ──────────────────────────────────────────────────────────

async def _get_watttime_token() -> str:
    """Return a cached Bearer token, refreshing if expired (30-min TTL)."""
    if _WT_TOKEN.get("token") and time.monotonic() < float(_WT_TOKEN.get("expires", 0)):
        return str(_WT_TOKEN["token"])

    creds = get_watttime_credentials()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{WATTTIME_BASE}/login",
            json={"username": creds["username"], "password": creds["password"]},
        )
        resp.raise_for_status()
        token = resp.json()["token"]

    _WT_TOKEN["token"] = token
    _WT_TOKEN["expires"] = time.monotonic() + 1800  # 30 min
    return token


async def _fetch_watttime(
    wt_zone: str,
) -> tuple[float, list[ForecastPoint]] | None:
    """Fetch current MOER + 24h forecast from WattTime.

    Returns (gco2_kwh, forecast) on success.
    Returns None if WattTime is unreachable or returns a free-tier index instead of real MOER.
    """
    try:
        token = await _get_watttime_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=10) as client:
            rt = await client.get(
                f"{WATTTIME_BASE}/signal-index",
                params={"signal_type": "co2_moer", "ba": wt_zone},
                headers=headers,
            )
            if rt.status_code != 200:
                return None

            data = rt.json()

            # Pro tier: `value` in lbs CO₂/MWh
            # Free tier: `percent` (0–100 index) — not usable as gCO₂/kWh, fall through
            if "value" not in data:
                return None

            current_gco2 = float(data["value"]) * _LBS_MWH_TO_GCO2_KWH

            # 24h forecast
            fc = await client.get(
                f"{WATTTIME_BASE}/forecast",
                params={"signal_type": "co2_moer", "ba": wt_zone},
                headers=headers,
            )
            forecast: list[ForecastPoint] = []
            if fc.status_code == 200:
                for pt in fc.json().get("forecast", []):
                    forecast.append(ForecastPoint(
                        ts=pt["point_time"],
                        gco2_kwh=float(pt["value"]) * _LBS_MWH_TO_GCO2_KWH,
                    ))

            return current_gco2, forecast

    except Exception:
        return None


# ── Electricity Maps helpers ──────────────────────────────────────────────────

async def _fetch_electricity_maps(
    em_zone: str,
) -> tuple[float, list[ForecastPoint]] | None:
    """Fetch current average carbon intensity + 24h forecast from Electricity Maps.

    Returns (gco2_kwh, forecast) on success, None on any failure.
    """
    try:
        key = get_electricity_maps_key()
        headers = {"auth-token": key}

        async with httpx.AsyncClient(timeout=10) as client:
            rt = await client.get(
                f"{ELECMAPS_BASE}/carbon-intensity/latest",
                params={"zone": em_zone},
                headers=headers,
            )
            if rt.status_code != 200:
                return None

            current_gco2 = float(rt.json()["carbonIntensity"])

            fc = await client.get(
                f"{ELECMAPS_BASE}/carbon-intensity/forecast",
                params={"zone": em_zone},
                headers=headers,
            )
            forecast: list[ForecastPoint] = []
            if fc.status_code == 200:
                for pt in fc.json().get("forecast", []):
                    forecast.append(ForecastPoint(
                        ts=pt["datetime"],
                        gco2_kwh=float(pt["carbonIntensity"]),
                    ))

            return current_gco2, forecast

    except Exception:
        return None


# ── MCP tool ──────────────────────────────────────────────────────────────────

async def get_carbon_intensity(region: str) -> CarbonIntensityResult:
    """Return current carbon intensity (gCO₂/kWh) and 24h forecast for an AWS region.

    Tries WattTime first (marginal signal); falls back to Electricity Maps (average).
    Results are cached for 10 minutes.
    """
    cache_entry = _CARBON_CACHE.get(region)
    if cache_entry and (time.monotonic() - cache_entry[0]) < _CACHE_TTL_SEC:
        return cache_entry[1]

    wt_zone = REGION_TO_WT_ZONE.get(region)
    em_zone = REGION_TO_EM_ZONE.get(region)

    if not wt_zone and not em_zone:
        raise ValueError(
            f"Unsupported region '{region}'. "
            "Add it to REGION_TO_WT_ZONE / REGION_TO_EM_ZONE in carbon_intensity.py."
        )

    # 1. Try WattTime (marginal signal)
    if wt_zone:
        wt_result = await _fetch_watttime(wt_zone)
        if wt_result:
            current, forecast = wt_result
            result = CarbonIntensityResult(
                region=region,
                zone=wt_zone,
                current_gco2_kwh=current,
                forecast=forecast,
                signal_type="marginal",
            )
            _CARBON_CACHE[region] = (time.monotonic(), result)
            return result

    # 2. Fall back to Electricity Maps (average signal)
    if em_zone:
        em_result = await _fetch_electricity_maps(em_zone)
        if em_result:
            current, forecast = em_result
            result = CarbonIntensityResult(
                region=region,
                zone=em_zone,
                current_gco2_kwh=current,
                forecast=forecast,
                signal_type="average",
            )
            _CARBON_CACHE[region] = (time.monotonic(), result)
            return result

    raise RuntimeError(
        f"No carbon intensity data available for region '{region}'. "
        "Check WATTTIME_USERNAME/PASSWORD and ELECTRICITY_MAPS_API_KEY are set."
    )
