#!/usr/bin/env python3
"""CDK app entry point — synthesizes the CCO infrastructure stack."""
import os
import aws_cdk as cdk
from infra.stacks.cco_stack import CCOStack

app = cdk.App()

CCOStack(
    app,
    "CCOStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT") or app.node.try_get_context("account"),
        region=os.environ.get("CDK_DEFAULT_REGION") or app.node.try_get_context("region") or "us-east-1",
    ),
    description="Carbon & Cost Optimizer — MCP Server, IAM, S3",
)

app.synth()
