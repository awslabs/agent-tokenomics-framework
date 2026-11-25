#!/usr/bin/env bash
#
# Deploy the application plane (support agent) -- all via CDK / CloudFormation.
#
# Flow (cloud-only, one tool):
#   1. Ensure the CDK environment is bootstrapped (idempotent).
#   2. cdk deploy the Data + AppPlane stacks (Cognito user pool, agent runtime,
#      Chat API + authorizer, chat UI hosting). The AppPlane stack builds the
#      ARM64 agent image from src/app-plane/Dockerfile via the
#      aws_bedrockagentcore.Runtime L2 -- no AgentCore CLI involved.
#   3. Build + publish the chat SPA with Cognito + Chat API config baked in.
#   4. Load demo data (seed/load_app_plane.py): create Cognito users, write their
#      orders keyed by sub, seed the FAQ, reset flags.
#
# Prereqs: authenticated AWS CLI, Node.js (npx), a running container engine
# (Docker/Finch) for the ARM64 image build, and Bedrock model access enabled.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/common/common.sh"   # CDK version, resolve_region, cfn_output, require_infra_venv

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
resolve_region   # -> REGION

# The CDK Python environment is created by scripts/deploy-all.sh (step 1).
# This single infra venv has aws-cdk-lib (for CDK) and boto3 (for seeding).
PYBIN="$(require_infra_venv "$ROOT")"
CDK_APP="$PYBIN app.py"
SEED_PY="$PYBIN"

export CDK_DEFAULT_ACCOUNT="$ACCOUNT" CDK_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
echo "[deploy-app-plane] account=$ACCOUNT region=$REGION"

# 1. Bootstrap (idempotent -- no-op if already bootstrapped).
echo "[deploy-app-plane] ensuring CDK bootstrap..."
( cd "$ROOT/infra" && $CDK bootstrap "aws://$ACCOUNT/$REGION" )

# 2. Deploy Data + AppPlane (builds the agent image + creates the runtime).
echo "[deploy-app-plane] deploying CDK stacks (Data, AppPlane)..."
( cd "$ROOT/infra" && $CDK deploy Tokenomics-Data Tokenomics-AppPlane \
    --app "$CDK_APP" --require-approval never )

# Read a named CloudFormation output from the AppPlane stack.
output () { cfn_output "$REGION" Tokenomics-AppPlane "$1"; }

RUNTIME_ARN="$(output AgentRuntimeArn)"
CHAT_API="$(output ChatApiUrl)"
USER_POOL_ID="$(output UserPoolId)"
USER_POOL_CLIENT_ID="$(output UserPoolClientId)"
ORDERS_TABLE="$(output OrdersTable)"
FAQ_TABLE="$(output FaqTable)"
FLAGS_TABLE="$(output FlagsTable)"
echo "[deploy-app-plane] agent runtime ARN: $RUNTIME_ARN"
echo "[deploy-app-plane] chat API: $CHAT_API"
echo "[deploy-app-plane] user pool: $USER_POOL_ID"

# 3. Build the chat SPA against the chat API + Cognito, then redeploy to publish.
( cd "$ROOT/web/app-plane" && npm install && \
    VITE_API_BASE="$CHAT_API" \
    VITE_USER_POOL_ID="$USER_POOL_ID" \
    VITE_USER_POOL_CLIENT_ID="$USER_POOL_CLIENT_ID" \
    VITE_AWS_REGION="$REGION" \
    npm run build )
echo "[deploy-app-plane] publishing chat UI..."
( cd "$ROOT/infra" && $CDK deploy Tokenomics-Data Tokenomics-AppPlane \
    --app "$CDK_APP" --require-approval never )

# 4. Load demo data: create the Cognito users, write their orders (keyed by sub),
#    seed the FAQ, reset flags. Uses the infra venv's boto3.
echo "[deploy-app-plane] loading demo data (Cognito users + orders + faq + flags)..."
"$SEED_PY" "$ROOT/seed/load_app_plane.py" \
  --region "$REGION" \
  --user-pool-id "$USER_POOL_ID" \
  --orders-table "$ORDERS_TABLE" \
  --faq-table "$FAQ_TABLE" \
  --flags-table "$FLAGS_TABLE"

CHAT_UI="$(aws cloudformation describe-stacks --region "$REGION" --stack-name Tokenomics-AppPlane \
  --query "Stacks[0].Outputs[?OutputKey=='ChatUiUrl'].OutputValue" --output text)"
echo
echo "[deploy-app-plane] Done."
echo "  Agent runtime ARN: $RUNTIME_ARN"
echo "  Chat API:          $CHAT_API"
echo "  Chat UI:           $CHAT_UI"
echo "  Demo logins:       bob@example.com / eva@example.com  (see seed/data/users.json)"
echo "  Next: scripts/deploy-control.sh"
