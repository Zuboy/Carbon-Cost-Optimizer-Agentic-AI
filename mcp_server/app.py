"""FastMCP server — exposes 4 carbon-aware training orchestration tools over streamable-HTTP."""
from fastmcp import FastMCP
from mcp_server.tools.spot_prices import get_spot_prices
from mcp_server.tools.carbon_intensity import get_carbon_intensity
from mcp_server.tools.training_job import launch_training_job
from mcp_server.tools.job_status import get_job_status

mcp = FastMCP("cco-server")

mcp.tool()(get_spot_prices)
mcp.tool()(get_carbon_intensity)
mcp.tool()(launch_training_job)
mcp.tool()(get_job_status)


def lambda_handler(event, context):
    """AWS Lambda entry point — streamable-HTTP MCP transport."""
    return mcp.run_lambda(event, context)
