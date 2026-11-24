"""ControlPlaneStack -- admin API + admin UI for business owners.

Provisioned ONCE. Feature toggles are DynamoDB writes, not stack changes.

  - Admin auth: a dedicated Cognito user pool (Tokenomics-control-plane) for
    business owners. Self sign-up is OFF -- admins are provisioned, not open
    registration. The admin API is behind this pool's JWT authorizer.
  - Lambda: packages src/control-plane + src/shared, entry lambda_entry.handler,
    fronted by an API Gateway proxy REST API. Reads/writes the DynamoDB tables
    and invokes the AgentCore runtime (referenced cross-stack from the app plane).
  - S3 + CloudFront host the admin SPA. CORS on the API is scoped to this
    distribution's origin (not a wildcard).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import jsii
from aws_cdk import (
    AssetHashType,
    BundlingOptions,
    CfnOutput,
    Duration,
    IgnoreMode,
    ILocalBundling,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as _lambda_sources  # noqa: F401 (reserved)
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_s3vectors as s3vectors
from aws_cdk import aws_sqs as sqs
from aws_cdk import custom_resources as cr
from constructs import Construct

from stacks import api_access_logs, auth_policy
from stacks.app_plane_stack import AppPlaneStack
from stacks.data_stack import DataStack

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
_WEB = _REPO_ROOT / "web"

# Model ids come from the shared price map (single source of truth), so infra
# and the agent never drift and nothing is hardcoded here.
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from shared import price_map  # noqa: E402


@jsii.implements(ILocalBundling)
class _ControlPlaneBundler:
    """Bundle the control-plane Lambda on the host (no Docker required).

    Copies src/control-plane to the asset root, vendors src/shared alongside it
    and adds the Advisor pattern catalog (read via config.ADVISOR_CATALOG_FILE).
    boto3 -- the only runtime dep -- is provided by the Lambda runtime, so no
    pip install is needed. The Advisor agent container (advisor/agent/) is built
    separately for AgentCore and is not shipped.
    """

    def try_bundle(self, output_dir: str, options) -> bool:
        out = Path(output_dir)
        cp = _SRC / "control-plane"
        shutil.copytree(cp, out, dirs_exist_ok=True)
        shutil.copytree(_SRC / "shared", out / "shared", dirs_exist_ok=True)
        shutil.rmtree(out / "advisor" / "agent", ignore_errors=True)
        (out / "advisor").mkdir(exist_ok=True)
        shutil.copy2(_ADVISOR_CATALOG, out / "advisor" / "tokenomics-patterns-kb.md")
        # Drop any local caches that shouldn't ship.
        for pyc in out.rglob("__pycache__"):
            shutil.rmtree(pyc, ignore_errors=True)
        return True


# ---- Tokenomics Advisor packaging -------------------------------------------
_ADVISOR_CATALOG = _REPO_ROOT / "seed" / "data" / "advisor" / "tokenomics-patterns-kb.md"
# The Advisor image builds from the repo root (the catalog lives in seed/), but only
# these paths reach the build context; the Dockerfile COPYs exactly these.
_ADVISOR_DOCKERFILE = "src/control-plane/advisor/agent/Dockerfile"
_ADVISOR_IMAGE_FILES = (
    "src/control-plane/advisor/agent",
    "src/control-plane/advisor/__init__.py",
    "src/control-plane/advisor/service",
    "src/control-plane/advisor/kb.py",
    "src/control-plane/advisor/catalog.py",
    "src/control-plane/advisor/data_tools.py",
    "src/control-plane/config.py",
    "src/control-plane/lib/__init__.py",
    "src/control-plane/lib/store.py",
    "src/control-plane/handlers/__init__.py",
    "src/control-plane/handlers/governance.py",
    "src/shared",
    "seed/data/advisor/tokenomics-patterns-kb.md",
)
_ADVISOR_DEFAULT_MODEL = price_map.SONNET_5_5  # single source of truth (shared/price_map)
_ADVISOR_EMBEDDING_MODEL = price_map.TITAN_EMBED_V2  # single source of truth (shared/price_map)


def _advisor_catalog_module():
    """Load advisor/catalog.py by path (pure; no control-plane config import)."""
    spec = importlib.util.spec_from_file_location(
        "advisor_catalog_synth", _SRC / "control-plane" / "advisor" / "catalog.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ControlPlaneStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, data: DataStack, app: AppPlaneStack, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        # The agent runtime ARN comes straight from the AppPlane stack (cross-
        # stack reference), so no manual ARN hand-off is needed.
        agent_runtime_arn = app.runtime.agent_runtime_arn

        # ---- Admin auth: dedicated Cognito pool (business owners) ----------
        # Self sign-up OFF -- admins are provisioned, not open registration.
        self.admin_pool = cognito.UserPool(
            self,
            "AdminUserPool",
            user_pool_name="Tokenomics-control-plane",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=auth_policy.password_policy(),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,  # demo resource
            **auth_policy.threat_protection_props(self),
        )
        self.admin_pool_client = self.admin_pool.add_client(
            "AdminSpaClient",
            user_pool_client_name="Tokenomics-control-plane-spa",
            auth_flows=auth_policy.spa_auth_flows(),
            prevent_user_existence_errors=True,
            generate_secret=False,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )

        # Control-plane roles (threat model M14; src/control-plane/lib/authz.py).
        # governance-admin may change flags / usage plans / overrides and start
        # benchmarks; viewer is read-only. Users in neither group get 403.
        for gid, name, desc in (
            ("GovernanceAdminGroup", "governance-admin", "Change governance config and run benchmarks"),
            ("ViewerGroup", "viewer", "Read-only access to analytics, config and audit"),
        ):
            cognito.CfnUserPoolGroup(
                self, gid, user_pool_id=self.admin_pool.user_pool_id, group_name=name, description=desc
            )

        # ---- Admin UI hosting (created first so we know its origin) --------
        site_bucket = s3.Bucket(
            self,
            "WebBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        # Explicit, region-qualified OAC name. CloudFront OAC names are unique
        # per account GLOBALLY, so the CDK-derived default name collides with
        # the same stack deployed in another region. Qualify it with the region.
        web_oac = cloudfront.S3OriginAccessControl(
            self,
            "WebOac",
            origin_access_control_name=f"Tokenomics-ControlPlane-Web-{self.region}",
        )
        distribution = cloudfront.Distribution(
            self,
            "WebDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    site_bucket,
                    origin_access_control=web_oac,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403, response_http_status=200, response_page_path="/index.html"
                ),
                cloudfront.ErrorResponse(
                    http_status=404, response_http_status=200, response_page_path="/index.html"
                ),
            ],
        )
        web_origin = f"https://{distribution.distribution_domain_name}"

        # Control-plane-owned data: benchmark runs.
        self.runs_table = dynamodb.Table(
            self,
            "BenchmarkRuns",
            partition_key=dynamodb.Attribute(name="run_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Token-governance metrics / ledger (control-plane owned). The metering
        # consumer (EventBridge target) writes roll-ups + enforcement events here;
        # the admin API reads them for plan-vs-actual, top-spenders, event log.
        # pk = SESSION#id | USER#id#DAY#date | AGENT#name#DAY#date | EVENT#ts#uuid.
        self.metrics_table = dynamodb.Table(
            self,
            "Metrics",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            # Only the Advisor's TURNS#<session_id> ledger rows carry `ttl` (90 days);
            # roll-up rows have no ttl attribute and never expire.
            time_to_live_attribute="ttl",
        )
        # GSI to query analytics by user + time window without a full-table scan.
        # Roll-up rows are tagged with `user_id` (partition) and `day` (sort) by
        # the metering consumer; the analytics handler queries this index.
        self.metrics_table.add_global_secondary_index(
            index_name="user-day-index",
            partition_key=dynamodb.Attribute(name="user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="day", type=dynamodb.AttributeType.STRING),
        )

        # Governance audit trail (threat model M16; src/control-plane/lib/audit.py):
        # one append-only record per change -- who, when, before/after.
        self.audit_table = dynamodb.Table(
            self,
            "GovernanceAudit",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,  # demo resource
        )

        # ---- API Lambda (control-plane handlers) ---------------------------
        # Bundle src/control-plane as the code root and vendor src/shared + deps
        # into it, so `import shared...` and `import handlers/lib/config` resolve.
        self.api_fn = lambda_.Function(
            self,
            "ControlPlaneApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambda_entry.handler",
            timeout=Duration.seconds(120),
            memory_size=512,
            # The only runtime dependency (boto3) is already present in the
            # Lambda Python runtime, so bundling is a pure code copy -- no pip,
            # no Docker. We vendor control-plane code at the asset root and the
            # shared package alongside it so imports resolve.
            code=lambda_.Code.from_asset(
                str(_SRC),
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    local=_ControlPlaneBundler(),
                    command=[
                        "bash",
                        "-c",
                        "cp -r control-plane/. /asset-output && cp -r shared /asset-output/shared",
                    ],
                ),
            ),
            environment={
                "FLAGS_TABLE": data.flags_table.table_name,
                "RUNS_TABLE": self.runs_table.table_name,
                "AGENT_RUNTIME_ARN": agent_runtime_arn,
                "ALLOWED_ORIGIN": web_origin,  # CORS scoped to the admin SPA
                # Token governance: shared usage-plans config (control writes) +
                # the metrics/ledger table the API reads for plan-vs-actual.
                "USAGE_PLANS_TABLE": data.usage_plans_table.table_name,
                "METRICS_TABLE": self.metrics_table.table_name,
                # App-plane pool id: resolve customer email -> sub for per-user overrides.
                "APP_USER_POOL_ID": app.user_pool.user_pool_id,
            },
        )

        # DynamoDB access: shared flags (read/write) + own runs table.
        data.flags_table.grant_read_write_data(self.api_fn)
        self.runs_table.grant_read_write_data(self.api_fn)
        # Token governance: the admin API authors usage plans (shared, read/write)
        # and reads the metrics/ledger for plan-vs-actual + event log.
        data.usage_plans_table.grant_read_write_data(self.api_fn)
        self.metrics_table.grant_read_data(self.api_fn)
        # Audit trail: append + read only -- the API can't update or delete records.
        self.api_fn.add_environment("AUDIT_TABLE", self.audit_table.table_name)
        self.api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem", "dynamodb:Query"],
                resources=[self.audit_table.table_arn],
            )
        )
        # Resolve customer email -> sub (per-user override) against the app pool.
        self.api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:AdminGetUser"],
                resources=[app.user_pool.user_pool_arn],
            )
        )

        # ---- Metering consumer: EventBridge -> Lambda -> metrics table ------
        # The app plane emits UsageRecorded events to its metering bus; this
        # control-plane Lambda consumes them and persists roll-ups + events.
        # (Bundled the same way as the API Lambda so `shared` + handlers resolve.)
        self.metering_consumer_fn = lambda_.Function(
            self,
            "MeteringConsumer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="metering_consumer.handler",
            timeout=Duration.seconds(30),
            memory_size=256,
            code=lambda_.Code.from_asset(
                str(_SRC),
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    local=_ControlPlaneBundler(),
                    command=[
                        "bash",
                        "-c",
                        "cp -r control-plane/. /asset-output && cp -r shared /asset-output/shared",
                    ],
                ),
            ),
            environment={
                # config.py imports require these (shared module); the consumer
                # only uses METRICS_TABLE but must satisfy config at import time.
                "FLAGS_TABLE": data.flags_table.table_name,
                "RUNS_TABLE": self.runs_table.table_name,
                "USAGE_PLANS_TABLE": data.usage_plans_table.table_name,
                "METRICS_TABLE": self.metrics_table.table_name,
            },
        )
        self.metrics_table.grant_read_write_data(self.metering_consumer_fn)

        # Subscribe the consumer to the app-plane metering bus. The consumer
        # routes by DetailType internally, so one rule matches all typed events:
        # UsageRecorded (rollups) + LimitViolation / QuotaBlock / ThrottleEvent
        # (violations, throttles, event log).
        events.Rule(
            self,
            "MeteringRule",
            event_bus=app.metering_bus,
            event_pattern=events.EventPattern(
                source=["tokenomics.appplane"],
                detail_type=["UsageRecorded", "LimitViolation", "QuotaBlock", "ThrottleEvent"],
            ),
            targets=[targets.LambdaFunction(self.metering_consumer_fn)],
        )

        # ---- Async benchmark worker Lambda ---------------------------------
        # Runs the N x M benchmark loop off the request Lambda (which freezes after
        # its 202). Same code bundle as the API fn so handlers/lib/shared/config
        # all resolve; invoked asynchronously (InvocationType="Event") by the API fn.
        self.benchmark_worker_fn = lambda_.Function(
            self,
            "BenchmarkWorker",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="benchmarking.worker.handler",
            timeout=Duration.minutes(15),
            memory_size=512,
            code=lambda_.Code.from_asset(
                str(_SRC),
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    local=_ControlPlaneBundler(),
                    command=[
                        "bash",
                        "-c",
                        "cp -r control-plane/. /asset-output && cp -r shared /asset-output/shared",
                    ],
                ),
            ),
            environment={
                # Mirror the API fn env so config.py imports cleanly. config.py REQUIRES
                # FLAGS_TABLE and RUNS_TABLE (os.environ[...] with no default) -> both MUST be set.
                "FLAGS_TABLE": data.flags_table.table_name,
                "RUNS_TABLE": self.runs_table.table_name,
                "AGENT_RUNTIME_ARN": agent_runtime_arn,
                "USAGE_PLANS_TABLE": data.usage_plans_table.table_name,
                "METRICS_TABLE": self.metrics_table.table_name,
                "APP_USER_POOL_ID": app.user_pool.user_pool_id,
                "BENCHMARK_MAC_KEY_ARN": app.benchmark_mac_key.key_arn,
            },
        )
        # The worker only reads/writes run docs (progress + COMPLETED/FAILED). It
        # does NOT touch the live Flags or UsagePlans tables: flags are pinned from
        # the worker event payload and governance is GovernanceConfig.empty(), so
        # those tables are never read or written on the benchmark path. The env
        # vars above remain so config.py imports cleanly, but NO DynamoDB grant is
        # given for them -- least privilege keeps the worker from mutating live
        # configuration and preserves the isolation boundary.
        self.runs_table.grant_read_write_data(self.benchmark_worker_fn)
        # The worker invokes the AgentCore runtime per turn, signing each request
        # (threat model M8). It is the ONLY principal that may generate MACs.
        app.runtime.grant_invoke(self.benchmark_worker_fn)
        app.benchmark_mac_key.grant(self.benchmark_worker_fn, "kms:GenerateMac")
        # Let the API fn async-invoke the worker, and tell it the function name.
        self.benchmark_worker_fn.grant_invoke(self.api_fn)
        self.api_fn.add_environment("BENCHMARK_WORKER_FUNCTION_NAME", self.benchmark_worker_fn.function_name)
        # No InvokeAgentRuntime grant on the API fn: only the async benchmark worker
        # calls the support runtime (least privilege, threat model T1/T2).

        self._add_advisor(data, app)

        # Admin JWT authorizer -> /flags + /benchmarks require an admin login.
        admin_authorizer = apigw.CognitoUserPoolsAuthorizer(
            self, "AdminAuthorizer", cognito_user_pools=[self.admin_pool]
        )
        self.api = apigw.LambdaRestApi(
            self,
            "ControlPlaneRestApi",
            handler=self.api_fn,
            proxy=True,
            # The admin SPA polls benchmarks / Advisor replies; 20 req/s leaves headroom.
            deploy_options=api_access_logs.stage_options(
                self, "ControlPlaneApiAccessLogs", rate_limit=20, burst_limit=40
            ),
            default_method_options=apigw.MethodOptions(
                authorizer=admin_authorizer,
                authorization_type=apigw.AuthorizationType.COGNITO,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=[web_origin],
                allow_methods=apigw.Cors.ALL_METHODS,
            ),
        )

        # Deploy the admin SPA if it's built (npm run build produces dist/).
        cp_dist = _WEB / "control-plane" / "dist"
        if cp_dist.is_dir():
            s3deploy.BucketDeployment(
                self,
                "WebDeploymentControlPlane",
                sources=[s3deploy.Source.asset(str(cp_dist))],
                destination_bucket=site_bucket,
                distribution=distribution,
                distribution_paths=["/*"],
            )

        # ---- Outputs -------------------------------------------------------
        CfnOutput(self, "ApiUrl", value=self.api.url)
        CfnOutput(self, "WebUrl", value=web_origin)
        CfnOutput(self, "WebBucketName", value=site_bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "AdminUserPoolId", value=self.admin_pool.user_pool_id)
        CfnOutput(self, "AdminUserPoolClientId", value=self.admin_pool_client.user_pool_client_id)
        # Token-governance table names (read by the seed script).
        CfnOutput(self, "UsagePlansTableName", value=data.usage_plans_table.table_name)
        CfnOutput(self, "MetricsTableName", value=self.metrics_table.table_name)
        CfnOutput(self, "AuditTableName", value=self.audit_table.table_name)
        CfnOutput(self, "AdvisorKbId", value=self.advisor_kb.attr_knowledge_base_id)
        CfnOutput(self, "AdvisorKbDataSourceId", value=self.advisor_kb_source.attr_data_source_id)
        CfnOutput(self, "AdvisorRuntimeArn", value=self.advisor_runtime.agent_runtime_arn)
        CfnOutput(self, "AdvisorChatsTableName", value=self.advisor_chats_table.table_name)
        self.site_bucket = site_bucket
        self.distribution = distribution

    # ---- Tokenomics Advisor ----------------------------------------------------
    def _add_advisor(self, data: DataStack, app: AppPlaneStack) -> None:
        """The Advisor: a Strands agent on its own AgentCore Runtime (its read-only data
        tools run in-process), an async worker, the AdvisorChats table, the
        pattern-catalog Knowledge Base (S3 Vectors) and a prompt-attack guardrail. The
        /advisor/chats routes run in the existing API Lambda (proxy), so no API Gateway
        change is needed."""
        account, region = self.account, self.region
        advisor_model = self.node.try_get_context("advisorModel") or _ADVISOR_DEFAULT_MODEL
        catalog = _advisor_catalog_module()
        catalog_md = _ADVISOR_CATALOG.read_text(encoding="utf-8")
        catalog_sha = hashlib.sha256(catalog_md.encode("utf-8")).hexdigest()

        # The worker Lambda's code. Hashing the bundle output (not the source) means a
        # catalog or code edit redeploys it, and the hash never collides with the API
        # bundle's source hash.
        advisor_code = lambda_.Code.from_asset(
            str(_SRC),
            asset_hash_type=AssetHashType.OUTPUT,
            bundling=BundlingOptions(
                image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                local=_ControlPlaneBundler(),
                command=["bash", "-c", "echo 'advisor bundles locally only' && exit 1"],
            ),
        )

        # One customer-managed key for Advisor data at rest: chat history (it holds
        # admin questions and answers), the Advisor log groups and Lambda env vars.
        advisor_key = kms.Key(
            self,
            "AdvisorKey",
            enable_key_rotation=True,
            description="Tokenomics Advisor: chats, logs and Lambda environment",
            removal_policy=RemovalPolicy.DESTROY,  # demo resource
        )
        advisor_key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal(f"logs.{region}.amazonaws.com")],
                actions=[
                    "kms:Encrypt*",
                    "kms:Decrypt*",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:Describe*",
                ],
                resources=["*"],
                conditions={
                    "ArnLike": {
                        "kms:EncryptionContext:aws:logs:arn": f"arn:aws:logs:{region}:{account}:log-group:*"
                    }
                },
            )
        )

        def log_group(cid: str) -> logs.LogGroup:
            return logs.LogGroup(
                self,
                cid,
                retention=logs.RetentionDays.ONE_MONTH,
                encryption_key=advisor_key,
                removal_policy=RemovalPolicy.DESTROY,
            )

        def no_vpc(fn: lambda_.Function, *extra: tuple[str, str]) -> None:
            """Documented scanner exceptions for the Advisor Lambdas."""
            rules = [
                (
                    "CKV_AWS_117",
                    "No VPC in this stack; the function only calls AWS APIs (DynamoDB, "
                    "Bedrock, Lambda) over IAM-authenticated public endpoints.",
                ),
                *extra,
            ]
            cfn = fn.node.default_child
            cfn.add_metadata("checkov", {"skip": [{"id": i, "comment": c} for i, c in rules]})
            cfn.add_metadata(
                "cfn_nag",
                {
                    "rules_to_suppress": [
                        {"id": "W89", "reason": rules[0][1]},
                        {
                            "id": "W58",
                            "reason": "Log permissions come from the CDK-managed basic execution role.",
                        },
                    ]
                },
            )

        # Conversations (per admin) + reply jobs. pk = USER#<admin sub>.
        self.advisor_chats_table = dynamodb.Table(
            self,
            "AdvisorChats",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=advisor_key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,  # demo resource
        )

        # ---- Pattern catalog Knowledge Base (Bedrock KB on S3 Vectors) ------------
        catalog_bucket = s3.Bucket(
            self,
            "AdvisorCatalogSource",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        catalog_bucket.node.default_child.add_metadata(
            "checkov",
            {
                "skip": [
                    {
                        "id": "CKV_AWS_18",
                        "comment": "Holds only the public pattern catalog (no customer data), written by CDK and read by Bedrock; "
                        "access logging would need a separate log bucket for no security gain.",
                    }
                ]
            },
        )
        catalog_bucket.node.default_child.add_metadata(
            "cfn_nag",
            {
                "rules_to_suppress": [
                    {"id": "W35", "reason": "Holds only the public pattern catalog (no customer data)."}
                ]
            },
        )
        sources = []
        for key, text, meta in catalog.split_catalog(catalog_md):
            sources.append(s3deploy.Source.data(f"catalog/{key}", text))
            sources.append(s3deploy.Source.data(f"catalog/{key}.metadata.json", meta))
        catalog_deploy = s3deploy.BucketDeployment(
            self, "AdvisorCatalogDeploy", sources=sources, destination_bucket=catalog_bucket
        )

        vector_bucket = s3vectors.CfnVectorBucket(self, "AdvisorVectors")
        vector_bucket.apply_removal_policy(RemovalPolicy.DESTROY)
        vector_index = s3vectors.CfnIndex(
            self,
            "AdvisorPatternIndex",
            vector_bucket_arn=vector_bucket.attr_vector_bucket_arn,
            data_type="float32",
            dimension=1024,
            distance_metric="cosine",
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
            ),
        )
        vector_index.apply_removal_policy(RemovalPolicy.DESTROY)

        embed_arn = f"arn:aws:bedrock:{region}::foundation-model/{_ADVISOR_EMBEDDING_MODEL}"
        kb_role = iam.Role(
            self,
            "AdvisorKbRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{region}:{account}:knowledge-base/*"},
                },
            ),
        )
        kb_role.add_to_policy(iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=[embed_arn]))
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:QueryVectors",
                    "s3vectors:GetIndex",
                ],
                resources=[vector_index.attr_index_arn],
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[catalog_bucket.bucket_arn],
                conditions={"StringEquals": {"aws:ResourceAccount": account}},
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[catalog_bucket.arn_for_objects("*")],
                conditions={"StringEquals": {"aws:ResourceAccount": account}},
            )
        )

        self.advisor_kb = bedrock.CfnKnowledgeBase(
            self,
            "AdvisorPatternsKb",
            name="TokenomicsAdvisorPatterns",
            description="Tokenomics pattern catalog (one document per pattern) for the Advisor.",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embed_arn,
                    embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                        bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase.BedrockEmbeddingModelConfigurationProperty(
                            dimensions=1024, embedding_data_type="FLOAT32"
                        )
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=vector_index.attr_index_arn
                ),
            ),
        )
        self.advisor_kb.node.add_dependency(kb_role)
        self.advisor_kb_source = bedrock.CfnDataSource(
            self,
            "AdvisorPatternsSource",
            name="tokenomics-patterns",
            knowledge_base_id=self.advisor_kb.attr_knowledge_base_id,
            data_deletion_policy="RETAIN",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=catalog_bucket.bucket_arn, inclusion_prefixes=["catalog/"]
                ),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="NONE"
                )
            ),
        )
        # (Re)ingest whenever the catalog changes -- physical id = catalog hash.
        ingest_call = cr.AwsSdkCall(
            service="bedrock-agent",
            action="StartIngestionJob",
            parameters={
                "knowledgeBaseId": self.advisor_kb.attr_knowledge_base_id,
                "dataSourceId": self.advisor_kb_source.attr_data_source_id,
            },
            physical_resource_id=cr.PhysicalResourceId.of(f"advisor-kb-{catalog_sha[:16]}"),
        )
        ingest = cr.AwsCustomResource(
            self,
            "AdvisorKbIngest",
            on_create=ingest_call,
            on_update=ingest_call,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["bedrock:StartIngestionJob"],
                        resources=[self.advisor_kb.attr_knowledge_base_arn],
                    )
                ]
            ),
            install_latest_aws_sdk=False,
        )
        ingest.node.add_dependency(catalog_deploy)
        ingest.node.add_dependency(self.advisor_kb_source)

        # ---- Guardrail: prompt-attack filter on the admin's input ---------------
        guardrail_cfg = {
            "blocked": "I can only help with this solution's token economics.",
            "filters": [{"type": "PROMPT_ATTACK", "input_strength": "HIGH", "output_strength": "NONE"}],
        }
        guardrail = bedrock.CfnGuardrail(
            self,
            "AdvisorGuardrail",
            name="TokenomicsAdvisorGuardrail",
            description="Blocks prompt-attack attempts in Advisor requests.",
            blocked_input_messaging=guardrail_cfg["blocked"],
            blocked_outputs_messaging=guardrail_cfg["blocked"],
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(**f) for f in guardrail_cfg["filters"]
                ]
            ),
        )
        # A version is an immutable snapshot: tie it to the guardrail's own config so
        # any policy change publishes a new version (and the runtime picks it up).
        guardrail_sha = hashlib.sha256(json.dumps(guardrail_cfg, sort_keys=True).encode()).hexdigest()
        guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "AdvisorGuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description=f"config {guardrail_sha[:12]}",
        )

        # ---- Advisor agent on its own AgentCore Runtime -------------------------
        self.advisor_runtime = agentcore.Runtime(
            self,
            "AdvisorAgentRuntime",
            runtime_name="TokenomicsAdvisorAgent",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_asset(
                str(_REPO_ROOT),
                file=_ADVISOR_DOCKERFILE,
                ignore_mode=IgnoreMode.DOCKER,
                exclude=["**", *(f"!{p}" for p in _ADVISOR_IMAGE_FILES), "**/__pycache__", "**/*.pyc"],
            ),
            description="Tokenomics Advisor (Strands): explains, analyses and recommends; read-only.",
            environment_variables={
                "ADVISOR_MODEL": advisor_model,
                # config.py requires FLAGS_TABLE + RUNS_TABLE at import time.
                "FLAGS_TABLE": data.flags_table.table_name,
                "RUNS_TABLE": self.runs_table.table_name,
                "USAGE_PLANS_TABLE": data.usage_plans_table.table_name,
                "METRICS_TABLE": self.metrics_table.table_name,
                "APP_USER_POOL_ID": app.user_pool.user_pool_id,
                "ADVISOR_KB_ID": self.advisor_kb.attr_knowledge_base_id,
                "ADVISOR_KB_MIN_SCORE": "0.2",
                "ADVISOR_GUARDRAIL_ID": guardrail.attr_guardrail_id,
                "ADVISOR_GUARDRAIL_VERSION": guardrail_version.attr_version,
            },
            # Same as the app-plane runtime: X-Ray delivery needs account-level
            # Transaction Search first; application logs still emit.
            tracing_enabled=False,
        )
        geo = advisor_model.split(".", 1)[0]
        base_model = (
            advisor_model.split(".", 1)[1] if geo in ("us", "eu", "apac", "global") else advisor_model
        )
        model_resources = [
            f"arn:aws:bedrock:*:{account}:inference-profile/{advisor_model}",
            f"arn:aws:bedrock:*::foundation-model/{base_model}",
        ]
        if geo == "global":
            # Global CRIS also needs the region-agnostic global FM ARN (no region,
            # no account) that authorizes the worldwide routing.
            model_resources.append(f"arn:aws:bedrock:::foundation-model/{base_model}")
        self.advisor_runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=model_resources,
            )
        )
        self.advisor_runtime.add_to_role_policy(
            iam.PolicyStatement(actions=["bedrock:ApplyGuardrail"], resources=[guardrail.attr_guardrail_arn])
        )
        # Read-only data for the in-process tools (same pattern as the app-plane runtime).
        self.metrics_table.grant_read_data(self.advisor_runtime.role)
        data.usage_plans_table.grant_read_data(self.advisor_runtime.role)
        data.flags_table.grant_read_data(self.advisor_runtime.role)
        self.advisor_runtime.add_to_role_policy(
            iam.PolicyStatement(actions=["cognito-idp:AdminGetUser"], resources=[app.user_pool.user_pool_arn])
        )
        self.advisor_runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve"], resources=[self.advisor_kb.attr_knowledge_base_arn]
            )
        )

        # ---- AdvisorWorker Lambda: answers one pending message asynchronously ---
        worker_fn = lambda_.Function(
            self,
            "AdvisorWorker",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="advisor.worker.handler",
            timeout=Duration.seconds(180),
            memory_size=512,
            code=advisor_code,
            log_group=log_group("AdvisorWorkerLogs"),
            environment_encryption=advisor_key,
            # Failed/expired async events land here for inspection (the reply is also
            # marked failed / released as stale, so the chat never stays locked).
            dead_letter_queue=sqs.Queue(
                self,
                "AdvisorWorkerDlq",
                retention_period=Duration.days(4),
                encryption=sqs.QueueEncryption.KMS,
                encryption_master_key=advisor_key,
                enforce_ssl=True,
            ),
            # Caps concurrent Advisor turns (and Bedrock spend) across all admins;
            # extra requests queue in the async invoke queue.
            reserved_concurrent_executions=3,
            environment={
                "FLAGS_TABLE": data.flags_table.table_name,
                "RUNS_TABLE": self.runs_table.table_name,
                "ADVISOR_CHATS_TABLE": self.advisor_chats_table.table_name,
                "ADVISOR_RUNTIME_ARN": self.advisor_runtime.agent_runtime_arn,
            },
        )
        # One delivery only: a retry would re-run (and re-bill) the agent; the claim
        # guard also refuses duplicate deliveries. Queue age (90 s) + timeout (180 s)
        # stays under the chat store's STALE_PENDING_SECONDS (300 s), so a turn never
        # starts after the UI has shown the reply as failed and unlocked the chat.
        worker_fn.configure_async_invoke(retry_attempts=0, max_event_age=Duration.seconds(90))
        no_vpc(worker_fn)
        self.advisor_chats_table.grant_read_write_data(worker_fn)
        self.advisor_runtime.grant_invoke(worker_fn)
        worker_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:StopRuntimeSession"],
                resources=[
                    self.advisor_runtime.agent_runtime_arn,
                    f"{self.advisor_runtime.agent_runtime_arn}/*",
                ],
            )
        )

        # ---- API Lambda: /advisor/chats routes (proxy) ---------------------------
        self.advisor_chats_table.grant_read_write_data(self.api_fn)
        worker_fn.grant_invoke(self.api_fn)
        self.api_fn.add_environment("ADVISOR_CHATS_TABLE", self.advisor_chats_table.table_name)
        self.api_fn.add_environment("ADVISOR_WORKER_FUNCTION_NAME", worker_fn.function_name)
