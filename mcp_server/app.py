"""FastMCP server — 4 carbon-aware training orchestration tools over streamable-HTTP.

Deployment:  AWS Lambda + API Gateway HTTP API (Mangum ASGI adapter)
Local dev:   python -m mcp_server.app   (runs uvicorn on :8000)
"""
from __future__ import annotations

from mangum import Mangum
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_server.tools.spot_prices import get_spot_prices
from mcp_server.tools.carbon_intensity import get_carbon_intensity
from mcp_server.tools.training_job import launch_training_job
from mcp_server.tools.job_status import get_job_status
from mcp_server.tools.scheduler import schedule_deferred_job, cancel_deferred_job

# ── FastMCP instance ──────────────────────────────────────────────────────────
mcp = FastMCP(
    name="cco-server",
    instructions=(
        "Carbon & Cost Optimizer MCP server. "
        "Use get_spot_prices and get_carbon_intensity to evaluate candidate regions, "
        "then launch_training_job to start the optimal SageMaker job immediately, "
        "or schedule_deferred_job to defer launch to a greener/cheaper future hour. "
        "Use cancel_deferred_job to cancel a pending deferred launch. "
        "Use get_job_status to monitor a running job."
    ),
)

mcp.tool()(get_spot_prices)
mcp.tool()(get_carbon_intensity)
mcp.tool()(launch_training_job)
mcp.tool()(get_job_status)
mcp.tool()(schedule_deferred_job)
mcp.tool()(cancel_deferred_job)


# ── Health route (registered directly on the FastMCP app) ─────────────────────
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "cco-mcp-server"})


# ── ASGI app + Lambda handler ─────────────────────────────────────────────────
# FastMCP 3.x: http_app(transport="streamable-http") returns a Starlette app
asgi_app = mcp.http_app(transport="streamable-http")

# Mangum translates API Gateway HTTP API v2 payload → ASGI
lambda_handler = Mangum(asgi_app, lifespan="off")


# ── Local dev runner ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mcp_server.app:asgi_app", host="0.0.0.0", port=8000, reload=True)
