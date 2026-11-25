#!/usr/bin/env bash
#
# Deploy the control plane (API + SPAs) -- all via CDK / CloudFormation.
#
# The ControlPlane stack references the AppPlane runtime through a CDK cross-
# stack reference, so there is no manual runtime-ARN hand-off. Deploy the app
# plane first (scripts/deploy-app-plane.sh); CDK enforces the dependency.
#
# Flow:
#   1. Deploy the stack once to learn the API URL.
#   2. Build both SPAs with VITE_API_BASE set to that URL.
#   3. Deploy again to publish the built SPA assets to S3/CloudFront.
#
# Prereqs: authenticated AWS CLI, Node.js (npm/npx).
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/common/common.sh"   # CDK version, resolve_region, cfn_output, require_infra_venv

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
resolve_region   # -> REGION

# The CDK Python environment is created by scripts/deploy-all.sh (step 1).
PYBIN="$(require_infra_venv "$ROOT")"
CDK_APP="$PYBIN app.py"

export CDK_DEFAULT_ACCOUNT="$ACCOUNT" CDK_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
echo "[deploy-control-plane] account=$ACCOUNT region=$REGION"

deploy_stacks () {
  ( cd "$ROOT/infra" && $CDK deploy Tokenomics-Data Tokenomics-AppPlane Tokenomics-ControlPlane \
      --app "$CDK_APP" --require-approval never )
}

output () { cfn_output "$REGION" Tokenomics-ControlPlane "$1"; }

build_admin_spa () {
  echo "[deploy-control-plane] building admin SPA (API=$API, pool=$ADMIN_POOL_ID)"
  ( cd "$ROOT/web/control-plane" && npm install && \
      VITE_API_BASE="$API" \
      VITE_ADMIN_USER_POOL_ID="$ADMIN_POOL_ID" \
      VITE_ADMIN_USER_POOL_CLIENT_ID="$ADMIN_CLIENT_ID" \
      VITE_AWS_REGION="$REGION" \
      npm run build )
}

echo "[deploy-control-plane] pass 1: deploy to obtain API URL + admin pool..."
deploy_stacks
API="$(output ApiUrl)"
ADMIN_POOL_ID="$(output AdminUserPoolId)"
ADMIN_CLIENT_ID="$(output AdminUserPoolClientId)"
echo "[deploy-control-plane] API URL: $API"

build_admin_spa
echo "[deploy-control-plane] pass 2: publish built admin SPA..."
deploy_stacks

# Tokenomics Advisor: the stack starts a Knowledge Base ingestion of the pattern
# catalog whenever the catalog changes; wait for it so the Advisor can retrieve
# patterns (until then it falls back to keyword matching over the same catalog).
wait_advisor_kb_ingestion () {
  local kb ds status
  kb="$(output AdvisorKbId)"; ds="$(output AdvisorKbDataSourceId)"
  [ -n "$kb" ] && [ -n "$ds" ] || return 0
  for _ in $(seq 1 60); do
    status="$(aws bedrock-agent list-ingestion-jobs --region "$REGION" --knowledge-base-id "$kb" \
      --data-source-id "$ds" --sort-by attribute=STARTED_AT,order=DESCENDING --max-results 1 \
      --query 'ingestionJobSummaries[0].status' --output text 2>/dev/null || echo UNKNOWN)"
    case "$status" in
      COMPLETE) echo "[deploy-control-plane] advisor KB ingestion complete"; return 0 ;;
      FAILED|STOPPED) echo "[deploy-control-plane] WARNING: advisor KB ingestion $status" >&2; return 0 ;;
    esac
    sleep 10
  done
  echo "[deploy-control-plane] WARNING: advisor KB ingestion still running; continuing" >&2
}
wait_advisor_kb_ingestion

# Create the Admin UI user (Cognito; self sign-up is off) with the admin password
# scripts/deploy-all.sh asked for, in the governance-admin group (may change
# config; threat model M14). With no password, an existing admin keeps theirs.
ADMIN_EMAIL="admin@example.com"
ADMIN_GROUP="governance-admin"
ADMIN_PW="${TOKENOMICS_ADMIN_PASSWORD:-}"
echo "[deploy-control-plane] creating admin user: $ADMIN_EMAIL ($ADMIN_GROUP)"
STATE="$(ensure_cognito_user "$REGION" "$ADMIN_POOL_ID" "$ADMIN_EMAIL" "$ADMIN_PW" "$ADMIN_GROUP")"
if [ "$STATE" = "exists" ]; then
  [ -n "$ADMIN_PW" ] && STATE="exists, password updated" || STATE="exists, password kept"
fi
echo "  $ADMIN_EMAIL: $STATE"

# Seed the token-governance tables (idempotent). Table names come from stack
# outputs; the app-plane pool id is accepted for the future per-user override.
echo "[deploy-control-plane] seeding governance..."
USAGE_PLANS_TABLE="$(output UsagePlansTableName)"
METRICS_TABLE="$(output MetricsTableName)"
APP_USER_POOL_ID="$(cfn_output "$REGION" Tokenomics-AppPlane UserPoolId)"
"$PYBIN" "$ROOT/seed/load_control_plane.py" --region "$REGION" \
  ${USAGE_PLANS_TABLE:+--usage-plans-table "$USAGE_PLANS_TABLE"} \
  ${METRICS_TABLE:+--metrics-table "$METRICS_TABLE"} \
  ${APP_USER_POOL_ID:+--app-user-pool-id "$APP_USER_POOL_ID"}

WEB_URL="$(output WebUrl)"

echo
echo "[deploy-control-plane] Done."
echo "  Admin API:   $API"
echo "  Admin UI:    $WEB_URL"
echo "  Admin login: $ADMIN_EMAIL"
echo "  (Chat UI is deployed by scripts/deploy-app-plane.sh)"
