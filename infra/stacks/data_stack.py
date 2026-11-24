"""DataStack -- only what BOTH planes share.

The single shared resource is the Flags table: the control plane writes it, the
agent (app plane) reads it. Everything plane-specific lives in its own stack:
  - Orders / Faq -> AppPlaneStack (customer profile lives in Cognito, no Customers table)
  - BenchmarkRuns            -> ControlPlaneStack

Keeping only Flags here means neither plane depends on the other for its own
data, and there is no circular stack dependency.
"""

from __future__ import annotations

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct


class DataStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        # Feature flags: one item per feature. Control plane writes; agent reads.
        self.flags_table = dynamodb.Table(
            self,
            "Flags",
            table_name="Tokenomics-Flags",  # fixed: shared + cross-stack exported
            partition_key=dynamodb.Attribute(name="feature", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,  # demo resource
        )

        # Usage plans / quotas (token governance): shared config, same contract as
        # Flags -- the control plane writes it, and the agent reads it for ALL
        # in-loop enforcement: per-agent caps, session cap, and the per-user
        # quota/throttle hook. One item per plan_id ("agent#<name>",
        # "user#default", "user#<sub>").
        self.usage_plans_table = dynamodb.Table(
            self,
            "UsagePlans",
            table_name="Tokenomics-UsagePlans",  # fixed: shared + cross-stack + seeded by name
            partition_key=dynamodb.Attribute(name="plan_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,  # demo resource
        )
