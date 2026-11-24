#!/usr/bin/env bash
#
# Shared helpers for the deploy scripts (sourced, not run directly).
# Keeps the region resolution, the pinned CDK version, and the CloudFormation
# output reader in ONE place so the two deploy scripts can't drift.
#
# Usage (from scripts/deploy-*.sh):
#   ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#   source "$ROOT/scripts/common/common.sh"
#   resolve_region          # sets REGION (env > infra/.deploy-region > default)
#   PYBIN="$(require_infra_venv "$ROOT")"
#   VALUE="$(cfn_output "$REGION" Tokenomics-AppPlane AgentRuntimeArn)"

# Pinned CDK CLI version (one source of truth for both deploy scripts).
CDK="npx --yes aws-cdk@2.1140.0"

# Default region when nothing else is set. Matches infra/app.py + src/*/config.py
# so an unset region lands every stack + the runtime in one region.
DEFAULT_REGION="us-east-1"

# resolve_region: set REGION from (in order) $AWS_REGION, infra/.deploy-region,
# else DEFAULT_REGION. Requires $ROOT to be set by the caller.
resolve_region () {
  local region_file="$ROOT/infra/.deploy-region"
  if [ -n "${AWS_REGION:-}" ]; then
    REGION="$AWS_REGION"
  elif [ -f "$region_file" ]; then
    REGION="$(tr -d '[:space:]' < "$region_file")"
  else
    REGION="$DEFAULT_REGION"
  fi
}

# require_infra_venv <root>: echo the CDK virtualenv python path, or fail with a
# clear message pointing at scripts/deploy-all.sh. The caller captures the stdout.
require_infra_venv () {
  local root="$1" pybin="$1/infra/.venv/bin/python"
  if [ ! -x "$pybin" ]; then
    echo "[deploy] ERROR: infra virtualenv not found at infra/.venv" >&2
    echo "  Run the full deploy first:  bash scripts/deploy-all.sh" >&2
    exit 1
  fi
  echo "$pybin"
}

# cfn_output <region> <stack> <output-key>: echo a CloudFormation stack output
# (empty string if absent; never fails the script).
cfn_output () {
  aws cloudformation describe-stacks --region "$1" --stack-name "$2" \
    --query "Stacks[0].Outputs[?OutputKey=='$3'].OutputValue" --output text 2>/dev/null || true
}
