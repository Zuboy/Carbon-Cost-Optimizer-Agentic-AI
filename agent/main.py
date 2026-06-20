"""Strands Agent entry point — carbon-aware SageMaker training orchestrator.

Full agent wiring with Bedrock AgentCore implemented in T-11.
"""
from __future__ import annotations
import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CCO Agent — carbon & cost-aware SageMaker training launcher"
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help='Training request, e.g. "train resnet50, deadline 6h, optimize for low carbon"',
    )
    parser.add_argument(
        "--mcp-url",
        default=os.environ.get("MCP_SERVER_URL"),
        help="MCP server endpoint URL (defaults to MCP_SERVER_URL env var)",
    )
    args = parser.parse_args()

    if not args.mcp_url:
        raise SystemExit(
            "ERROR: MCP server URL not set. Run `make deploy` and set MCP_SERVER_URL in .env"
        )

    # TODO (T-11): initialise Strands Agent with MCPClient + Bedrock Claude model
    print(f"[CCO Agent] Prompt   : {args.prompt}")
    print(f"[CCO Agent] MCP URL  : {args.mcp_url}")
    print("[CCO Agent] Agent not yet wired — implement in T-11")


if __name__ == "__main__":
    main()
