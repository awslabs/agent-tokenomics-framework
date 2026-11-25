"""AppPlaneStack -- the support agent + everything the app plane owns.

Owns:
  - Auth: a Cognito user pool (self sign-up off, custom:tier) + SPA client. User
    profile lives in Cognito; there is no Customers table.
  - Its business data: Orders / Faq (DynamoDB). Orders are keyed by the user's
    Cognito `sub`.
  - The Strands agent on AgentCore Runtime (built from src/app-plane/Dockerfile
    via the aws_bedrockagentcore.Runtime L2 -> image build + ECR + runtime).
  - The customer chat UI hosting (S3 + CloudFront). The chat SPA logs in via
    Cognito and calls the Chat API with a JWT.

Reads the shared Flags table (from DataStack) so the agent picks up toggles.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from stacks import api_access_logs, auth_policy
from stacks.data_stack import DataStack

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _REPO_ROOT / "src"  # docker build context (so the image can COPY shared)
_APP_PLANE_DIR = _SRC_DIR / "app-plane"
_CHAT_API_DIR = _APP_PLANE_DIR / "chat_api"  # thin proxy Lambda (boto3 only)
_CHAT_DIST = _REPO_ROOT / "web" / "app-plane" / "dist"

# Model ids come from the shared price map (single source of truth), so infra
# and the agent never drift and nothing is hardcoded here.
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
from shared.price_map import known_model_ids  # noqa: E402


class AppPlaneStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, data: DataStack, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        account = self.account

        # Optional per-environment model overrides via CDK context (else the
        # agent uses its config.py defaults). Nothing is hardcoded here.
        #   cdk deploy ... -c orchestratorModel=... -c knowledgeModel=... -c orderModel=...
        model_env = {}
        for env_key, ctx_key in (
            ("ORCHESTRATOR_MODEL", "orchestratorModel"),
            ("KNOWLEDGE_MODEL", "knowledgeModel"),
            ("ORDER_MODEL", "orderModel"),
        ):
            override = self.node.try_get_context(ctx_key)
            if override:
                model_env[env_key] = override

        common = {
            "billing_mode": dynamodb.BillingMode.PAY_PER_REQUEST,
            "removal_policy": RemovalPolicy.DESTROY,
        }

        # ---- Auth: Cognito user pool (profile store + login) ---------------
        # Self sign-up with email verification; `custom:tier` holds prime/basic.
        # The user profile (name, email, tier) lives here -- no Customers table.
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="Tokenomics-app-plane",
            # Self sign-up is disabled; customers are provisioned by the deploy
            # script (scripts/deploy-app-plane.sh).
            # TODO: re-enable (set True) only once the pool has protections for
            # open self sign-up in place (e.g. threat protection / WAF), and
            # restore the register entry point in the chat SPA (SELF_SIGNUP_ENABLED).
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                fullname=cognito.StandardAttribute(required=False, mutable=True),
            ),
            custom_attributes={
                "tier": cognito.StringAttribute(min_len=1, max_len=16, mutable=True),
            },
            password_policy=auth_policy.password_policy(),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,  # demo resource
            **auth_policy.threat_protection_props(self),
        )

        # Public SPA client: no secret; SRP only (no plaintext password flow).
        self.user_pool_client = self.user_pool.add_client(
            "SpaClient",
            user_pool_client_name="Tokenomics-app-plane-spa",
            auth_flows=auth_policy.spa_auth_flows(),
            prevent_user_existence_errors=True,
            generate_secret=False,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )

        # ---- App-plane business data (Orders keyed by Cognito sub) ---------
        # No explicit table_name: the key schema changed from the original
        # design, and a custom-named table can't be replaced in place. CDK
        # auto-names it; the agent reads the name from the ORDERS_TABLE env var.
        self.orders_table = dynamodb.Table(
            self,
            "Orders",
            partition_key=dynamodb.Attribute(name="user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="order_id", type=dynamodb.AttributeType.STRING),
            **common,
        )
        self.faq_table = dynamodb.Table(
            self,
            "Faq",
            partition_key=dynamodb.Attribute(name="id", type=dynamodb.AttributeType.STRING),
            **common,
        )

        # ---- AgentCore Memory (short-term) ---------------------------------
        # Short-term only for V1: raw conversation events, no long-term
        # strategies (those need an execution role + async extraction -> V2).
        # Event retention is context-driven (no hardcoded magic number).
        #   cdk deploy ... -c memoryEventExpiryDays=30
        memory_expiry_days = int(self.node.try_get_context("memoryEventExpiryDays") or 30)
        self.memory = agentcore.CfnMemory(
            self,
            "SupportAgentMemory",
            name="TokenomicsSupportAgentMemory",
            event_expiry_duration=memory_expiry_days,
            description="Short-term conversation memory for the support agent (flag-gated lever).",
        )
        self.memory.apply_removal_policy(RemovalPolicy.DESTROY)  # demo resource

        # ---- Token governance resources (created before the runtime so their
        #      names can be injected as runtime env vars) --------------------
        # Metering bus: the agent emits one usage event per turn; the control
        # plane consumes it. App plane only PUTs -- never reads/aggregates.
        self.metering_bus = events.EventBus(self, "MeteringBus", event_bus_name="Tokenomics-Metering")

        # Enforcement counter: the agent's authoritative per-user spend counters
        # for the in-loop quota/throttle hook (USER#<sub>#DAY/MONTH/MIN rows) and
        # the soft per-session cap (SESSION#<id>). Working state, distinct from the
        # control-plane metrics/ledger. TTL self-expires date/minute rows.
        self.enforcement_counter_table = dynamodb.Table(
            self,
            "EnforcementCounter",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="ttl",
            **common,
        )

        # Benchmark request signing (threat model M8): the control-plane benchmark
        # worker MACs each runtime request with this key and the runtime verifies
        # it, so only signed requests may use synthetic users or pin flags /
        # usage plans. Grants: runtime VerifyMac (below), worker GenerateMac
        # (ControlPlaneStack). HMAC keys don't support automatic rotation.
        self.benchmark_mac_key = kms.Key(
            self,
            "BenchmarkMacKey",
            key_spec=kms.KeySpec.HMAC_256,
            key_usage=kms.KeyUsage.GENERATE_VERIFY_MAC,
            description="Tokenomics: signs benchmark requests to the support agent runtime",
            removal_policy=RemovalPolicy.DESTROY,  # demo resource
        )
        self.benchmark_mac_key.node.default_child.add_metadata(
            "checkov",
            {
                "skip": [
                    {
                        "id": "CKV_AWS_7",
                        "comment": "HMAC KMS keys don't support automatic key rotation.",
                    }
                ]
            },
        )

        # ---- Guardrail on the customer-facing orchestrator (threat model T5) ----
        # Prompt-attack (jailbreak / injection) filter on the customer's input, plus
        # harmful-content filters both ways. INSULTS is output-only: angry customers
        # are normal for support and must not be blocked.
        guardrail_cfg = {
            "blocked": "Sorry, I can't help with that. I can help with your orders and our store policies.",
            "filters": [
                {"type": "PROMPT_ATTACK", "input_strength": "HIGH", "output_strength": "NONE"},
                {"type": "HATE", "input_strength": "MEDIUM", "output_strength": "MEDIUM"},
                {"type": "SEXUAL", "input_strength": "MEDIUM", "output_strength": "MEDIUM"},
                {"type": "VIOLENCE", "input_strength": "MEDIUM", "output_strength": "MEDIUM"},
                {"type": "MISCONDUCT", "input_strength": "MEDIUM", "output_strength": "MEDIUM"},
                {"type": "INSULTS", "input_strength": "NONE", "output_strength": "MEDIUM"},
            ],
        }
        guardrail = bedrock.CfnGuardrail(
            self,
            "SupportGuardrail",
            name="TokenomicsSupportGuardrail",
            description="Blocks prompt attacks and harmful content in the customer-support agent.",
            blocked_input_messaging=guardrail_cfg["blocked"],
            blocked_outputs_messaging=guardrail_cfg["blocked"],
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(**f) for f in guardrail_cfg["filters"]
                ]
            ),
        )
        # A version is an immutable snapshot: tie it to the config so any policy
        # change publishes a new version (and the runtime picks it up).
        guardrail_sha = hashlib.sha256(json.dumps(guardrail_cfg, sort_keys=True).encode()).hexdigest()
        guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "SupportGuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description=f"config {guardrail_sha[:12]}",
        )

        # ---- Agent runtime (image built from the Dockerfile) ---------------
        self.runtime = agentcore.Runtime(
            self,
            "SupportAgentRuntime",
            runtime_name="TokenomicsSupportAgent",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_asset(
                str(_SRC_DIR), file="app-plane/Dockerfile"
            ),
            description="Tokenomics customer-support agent (Strands orchestrator + specialists).",
            environment_variables={
                "ORDERS_TABLE": self.orders_table.table_name,
                "FAQ_TABLE": self.faq_table.table_name,
                "FLAGS_TABLE": data.flags_table.table_name,
                # AgentCore Memory resource id (short-term). Optional in the
                # agent: empty => stateless. The `memory` flag gates its use.
                "AGENTCORE_MEMORY_ID": self.memory.attr_memory_id,
                # Token governance: shared usage-plans config (in-loop caps) +
                # the metering bus the agent emits per-turn usage events to.
                "USAGE_PLANS_TABLE": data.usage_plans_table.table_name,
                "METERING_BUS": self.metering_bus.event_bus_name,
                # Enforcement counter: the agent reads/writes the per-user
                # quota/throttle rows (USER#<sub>#DAY/MONTH/MIN) + the soft
                # per-session cap row (SESSION#<id>).
                "ENFORCEMENT_COUNTER_TABLE": self.enforcement_counter_table.table_name,
                # Caller verification (tokenomics/caller_auth.py, threat model M8):
                # customer turns carry an id token from this pool + client;
                # benchmark turns carry a MAC from the benchmark key.
                "COGNITO_USER_POOL_ID": self.user_pool.user_pool_id,
                "COGNITO_CLIENT_ID": self.user_pool_client.user_pool_client_id,
                "BENCHMARK_MAC_KEY_ARN": self.benchmark_mac_key.key_arn,
                # Customer-facing guardrail on the orchestrator (threat model T5).
                "SUPPORT_GUARDRAIL_ID": guardrail.attr_guardrail_id,
                "SUPPORT_GUARDRAIL_VERSION": guardrail_version.attr_version,
                # Per-agent models: the agent reads its defaults from
                # src/app-plane/config.py (single source of truth). Only set an
                # env var here when an explicit per-environment override is given
                # via CDK context, e.g. -c orchestratorModel=... So nothing is
                # hardcoded in infra.
                **model_env,
            },
            # X-Ray trace delivery requires CloudWatch Transaction Search to be
            # enabled at the account level first (UpdateTraceSegmentDestination).
            # Leave tracing off until that one-time setup is done; AgentCore
            # still emits application logs. See architecture spec observability.
            tracing_enabled=False,
        )

        # Bedrock model access. Scope to exactly the models we use (no wildcard).
        # An inference profile resolves to the underlying foundation model, so each
        # model needs BOTH its profile ARN and its FM ARN granted.
        #   - `us.` / regional profiles: account-scoped profile ARN + regional FM ARN.
        #   - `global.` profiles (Global cross-Region inference): additionally need
        #     the REGION-AGNOSTIC global FM ARN `arn:aws:bedrock:::foundation-model/<m>`
        #     (no region, no account) -- that is what authorizes the worldwide routing.
        #     See "IAM policy requirements for global cross-Region inference".
        global_ids = [mid for mid in known_model_ids() if mid.startswith("global.")]
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:*:{account}:inference-profile/{mid}" for mid in known_model_ids()
                ]
                + [
                    # Regional FM ARN. Strip the cross-region prefix (e.g. "us."/"global.").
                    f"arn:aws:bedrock:*::foundation-model/{mid.split('.', 1)[-1]}"
                    for mid in known_model_ids()
                ]
                + [
                    # Region-agnostic global FM ARN -- required for Global CRIS routing.
                    f"arn:aws:bedrock:::foundation-model/{mid.split('.', 1)[-1]}"
                    for mid in global_ids
                ],
            )
        )

        self.runtime.add_to_role_policy(
            iam.PolicyStatement(actions=["bedrock:ApplyGuardrail"], resources=[guardrail.attr_guardrail_arn])
        )

        # Verify (never generate) benchmark request MACs.
        self.benchmark_mac_key.grant(self.runtime.role, "kms:VerifyMac")

        # DynamoDB reads: own business data + the shared flags table.
        self.orders_table.grant_read_data(self.runtime.role)
        self.faq_table.grant_read_data(self.runtime.role)
        data.flags_table.grant_read_data(self.runtime.role)

        # ---- Token governance (app-plane side) -----------------------------
        # The agent reads usage plans (in-loop caps) + emits metering events.
        # (The metering bus + enforcement counter are created before the runtime;
        # see the governance block just above the Runtime construct.)
        data.usage_plans_table.grant_read_data(self.runtime.role)
        self.metering_bus.grant_put_events_to(self.runtime.role)
        # Enforcement counter: the agent maintains the per-user quota/throttle
        # rows (USER#<sub>#DAY/MONTH/MIN) + the soft per-session cap (SESSION#<id>).
        self.enforcement_counter_table.grant_read_write_data(self.runtime.role)

        # AgentCore Memory data-plane: the agent writes conversation events and
        # reads prior turns back. Scoped to this memory resource (no wildcard).
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    # When the guardrail blocks a message, Strands rewrites it in
                    # history (redaction): create the replacement, delete the
                    # original. Without this the blocked text would stay in Memory
                    # and be replayed to the model on later turns.
                    "bedrock-agentcore:DeleteEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                ],
                resources=[self.memory.attr_memory_arn],
            )
        )

        # ---- Customer chat UI hosting (created first so we know its origin) -
        chat_bucket = s3.Bucket(
            self,
            "ChatWebBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        # Explicit, region-qualified OAC name. CloudFront OAC names are unique
        # per account GLOBALLY, so the CDK-derived default name collides with
        # the same stack deployed in another region. Qualify it with the region.
        chat_oac = cloudfront.S3OriginAccessControl(
            self,
            "ChatWebOac",
            origin_access_control_name=f"Tokenomics-AppPlane-ChatWeb-{self.region}",
        )
        chat_distribution = cloudfront.Distribution(
            self,
            "ChatWebDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    chat_bucket,
                    origin_access_control=chat_oac,
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
        if _CHAT_DIST.is_dir():
            s3deploy.BucketDeployment(
                self,
                "ChatWebDeployment",
                sources=[s3deploy.Source.asset(str(_CHAT_DIST))],
                destination_bucket=chat_bucket,
                distribution=chat_distribution,
                distribution_paths=["/*"],
            )
        chat_origin = f"https://{chat_distribution.distribution_domain_name}"

        # ---- Chat API (thin proxy the chat SPA calls, JWT-authorized) ------
        # A static SPA can't safely hold AWS creds to call InvokeAgentRuntime,
        # so this app-plane-owned Lambda invokes the runtime on its behalf.
        chat_fn = lambda_.Function(
            self,
            "ChatApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(_CHAT_API_DIR)),
            timeout=Duration.seconds(120),
            memory_size=256,
            environment={
                "AGENT_RUNTIME_ARN": self.runtime.agent_runtime_arn,
                "ALLOWED_ORIGIN": chat_origin,  # CORS scoped to the chat SPA
            },
        )
        self.runtime.grant_invoke(chat_fn)

        # The chat SPA logs in via Cognito and sends the JWT; the authorizer
        # validates it so /chat requires a signed-in user.
        chat_authorizer = apigw.CognitoUserPoolsAuthorizer(
            self, "ChatAuthorizer", cognito_user_pools=[self.user_pool]
        )
        chat_api = apigw.LambdaRestApi(
            self,
            "ChatRestApi",
            handler=chat_fn,
            proxy=True,
            # Each chat turn is one model-backed request; 10 req/s is ample for the demo.
            deploy_options=api_access_logs.stage_options(
                self, "ChatApiAccessLogs", rate_limit=10, burst_limit=20
            ),
            default_method_options=apigw.MethodOptions(
                authorizer=chat_authorizer,
                authorization_type=apigw.AuthorizationType.COGNITO,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=[chat_origin], allow_methods=apigw.Cors.ALL_METHODS
            ),
        )

        CfnOutput(self, "AgentRuntimeArn", value=self.runtime.agent_runtime_arn)
        CfnOutput(self, "MemoryId", value=self.memory.attr_memory_id)
        CfnOutput(self, "MeteringBusName", value=self.metering_bus.event_bus_name)
        CfnOutput(self, "EnforcementCounterTable", value=self.enforcement_counter_table.table_name)
        CfnOutput(self, "BenchmarkMacKeyArn", value=self.benchmark_mac_key.key_arn)
        CfnOutput(self, "ChatApiUrl", value=chat_api.url)
        CfnOutput(self, "ChatUiUrl", value=f"https://{chat_distribution.distribution_domain_name}")
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
        CfnOutput(self, "OrdersTable", value=self.orders_table.table_name)
        CfnOutput(self, "FaqTable", value=self.faq_table.table_name)
        CfnOutput(self, "FlagsTable", value=data.flags_table.table_name)
