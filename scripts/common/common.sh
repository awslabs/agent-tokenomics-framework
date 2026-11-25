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
CDK="npx --yes aws-cdk@2.1149.0"

# Default region when nothing else is set. Matches infra/app.py + src/*/config.py
# so an unset region lands every stack + the runtime in one region.
DEFAULT_REGION="us-east-1"

# Regions the solution supports (one source of truth for all deploy scripts).
# The Haiku 5.5 and Nova Lite model ids use `us.` cross-Region inference
# profiles (shared/price_map.py), which work only from US Regions, and
# AgentCore Memory is not available in us-west-1.
SUPPORTED_REGIONS="us-east-1 us-east-2 us-west-2"

# region_label <region>: echo the Region's display name.
region_label () {
  case "$1" in
    us-east-1) echo "US East (N. Virginia)" ;;
    us-east-2) echo "US East (Ohio)" ;;
    us-west-2) echo "US West (Oregon)" ;;
    *) echo "" ;;
  esac
}

# region_supported <region>: exit 0 if the region is in SUPPORTED_REGIONS.
region_supported () {
  local r
  for r in $SUPPORTED_REGIONS; do
    [ "$r" = "$1" ] && return 0
  done
  return 1
}

# require_supported_region <region>: fail with the supported list if not supported.
require_supported_region () {
  if ! region_supported "$1"; then
    echo "[deploy] ERROR: region '$1' is not supported. Supported: ${SUPPORTED_REGIONS// /, }." >&2
    echo "  Set AWS_REGION to one of them, or run: bash scripts/deploy-all.sh" >&2
    exit 1
  fi
}

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

# Cognito password policy shared by both pools (infra/stacks/auth_policy.py).
PASSWORD_POLICY_TEXT="12+ characters, with at least one uppercase letter, one lowercase letter, one digit and one symbol"

# password_ok <password>: exit 0 if it meets the Cognito pool policy. The
# password goes in on stdin (never as a process argument). Symbols are the
# Cognito set: https://docs.aws.amazon.com/cognito/latest/developerguide/managing-users-passwords.html
password_ok () {
  printf '%s' "$1" | python3 -c '
import sys
p = sys.stdin.read()
symbols = set("^$*.[]{}()?\"!@#%&/\\,><:;|_~`=+-" + chr(39))
ok = (
    12 <= len(p) <= 256
    and p == p.strip()
    and any("a" <= c <= "z" for c in p)
    and any("A" <= c <= "Z" for c in p)
    and any("0" <= c <= "9" for c in p)
    and any(c in symbols for c in p)
)
sys.exit(0 if ok else 1)'
}

# ensure_cognito_user <region> <pool-id> <email> <password> <group> [attr=value ...]
# Create the user if missing (no invite email), else update its attributes; set
# a permanent password; optionally add it to <group> ("" for none). Prints
# "created" or "exists". An empty <password> keeps an existing user's password
# and fails for a missing user. The password reaches boto3 on stdin, so it never
# appears in the process list or on disk. Needs $PYBIN (the infra venv python).
ensure_cognito_user () {
  local region="$1" pool="$2" email="$3" pw="$4" group="$5" attrs state
  shift 5
  attrs="$(python3 -c '
import json, sys
email, *pairs = sys.argv[1:]
attrs = [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}]
for p in pairs:
    k, v = p.split("=", 1)
    attrs.append({"Name": k, "Value": v})
print(json.dumps(attrs))' "$email" "$@")"

  if aws cognito-idp admin-get-user --region "$region" --user-pool-id "$pool" \
      --username "$email" >/dev/null 2>&1; then
    state="exists"
    aws cognito-idp admin-update-user-attributes --region "$region" --user-pool-id "$pool" \
      --username "$email" --user-attributes "$attrs" >/dev/null || return 1
  else
    [ -n "$pw" ] || { echo "[deploy] ERROR: $email does not exist and no password was given. Run: ./scripts/deploy-all.sh" >&2; return 1; }
    state="created"
    aws cognito-idp admin-create-user --region "$region" --user-pool-id "$pool" \
      --username "$email" --user-attributes "$attrs" --message-action SUPPRESS >/dev/null || return 1
  fi

  # Callers capture stdout via $(...), where bash turns off `set -e`, so every
  # step returns its failure explicitly.
  if [ -n "$pw" ]; then
    # boto3 from the infra venv: the AWS CLI can't read --cli-input-json from a
    # pipe, and passing --password would expose it in the process list.
    printf '%s' "$pw" | "${PYBIN:-$ROOT/infra/.venv/bin/python}" -c '
import sys, boto3
boto3.client("cognito-idp", region_name=sys.argv[1]).admin_set_user_password(
    UserPoolId=sys.argv[2], Username=sys.argv[3], Password=sys.stdin.read(), Permanent=True)' \
      "$region" "$pool" "$email" || return 1
  fi
  if [ -n "$group" ]; then
    aws cognito-idp admin-add-user-to-group --region "$region" --user-pool-id "$pool" \
      --username "$email" --group-name "$group" || return 1
  fi
  echo "$state"
}
