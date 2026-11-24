#!/usr/bin/env python3
"""CDK app entrypoint for the Tokenomics Reference Solution (cloud-only).

The stacks provision infrastructure ONCE (per the architecture's "provision
once, toggle by flag" rule) -- nothing here is created on a feature toggle.

Stacks & deploy order:
  1. DataStack         -> the shared Flags DynamoDB table only.
  2. AppPlaneStack     -> Cognito user pool, the AgentCore Runtime (image built
                          + created in-stack via the aws_bedrockagentcore.Runtime
                          L2), Orders/Faq tables, Chat API + authorizer, chat UI.
  3. ControlPlaneStack -> admin API (API Gateway + Lambda), BenchmarkRuns table,
                          admin UI. References the app-plane runtime via a CDK
                          cross-stack reference (no manual ARN hand-off).
"""

import os

import aws_cdk as cdk
from stacks.app_plane_stack import AppPlaneStack
from stacks.control_plane_stack import ControlPlaneStack
from stacks.data_stack import DataStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

data = DataStack(app, "Tokenomics-Data", env=env)
app_plane = AppPlaneStack(app, "Tokenomics-AppPlane", data=data, env=env)
ControlPlaneStack(app, "Tokenomics-ControlPlane", data=data, app=app_plane, env=env)

app.synth()
