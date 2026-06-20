"""Smoke tests for the FastMCP server — tool registration and health endpoint."""
import pytest
from starlette.testclient import TestClient

from mcp_server.app import asgi_app, mcp


# ── Health endpoint ───────────────────────────────────────────────────────────

def test_health_returns_200():
    with TestClient(asgi_app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200


def test_health_body():
    with TestClient(asgi_app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "cco-mcp-server"


# ── Tool registration ─────────────────────────────────────────────────────────

EXPECTED_TOOLS = {
    "get_spot_prices",
    "get_carbon_intensity",
    "launch_training_job",
    "get_job_status",
}


@pytest.mark.asyncio
async def test_all_tools_registered():
    tools = await mcp.list_tools()
    registered = {t.name for t in tools}
    assert EXPECTED_TOOLS == registered, (
        f"Missing: {EXPECTED_TOOLS - registered}  |  Extra: {registered - EXPECTED_TOOLS}"
    )


@pytest.mark.asyncio
async def test_tools_have_descriptions():
    tools = await mcp.list_tools()
    for tool in tools:
        assert tool.description, f"Tool '{tool.name}' is missing a description"


# ── App structure ─────────────────────────────────────────────────────────────

def test_asgi_app_is_not_none():
    assert asgi_app is not None


def test_lambda_handler_is_callable():
    from mcp_server.app import lambda_handler
    assert callable(lambda_handler)
