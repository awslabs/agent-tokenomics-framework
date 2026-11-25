#!/usr/bin/env bash
#
# Remove everything the Agent Tokenomics Framework deployed -- and nothing else.
#
#   bash scripts/destroy-all.sh            # asks you to type the region to confirm
#   bash scripts/destroy-all.sh --yes      # no prompt (CI); still Tokenomics-only
#
# What it deletes (Tokenomics resources only, in this account + region):
#   1. The three CloudFormation stacks, in reverse dependency order:
#        Tokenomics-ControlPlane -> Tokenomics-AppPlane -> Tokenomics-Data
#      Every resource in them is created with RemovalPolicy.DESTROY (and S3
#      auto-delete), so tables, buckets, user pools, AgentCore runtimes + memory,
#      the knowledge base and its vector store all go with the stacks. KMS keys
#      enter their standard pending-deletion window.
#   2. The CloudWatch log groups the services create at runtime, outside
#      CloudFormation, matched by these exact name prefixes only:
#        /aws/lambda/Tokenomics-{Data,AppPlane,ControlPlane}-
#        /aws/bedrock-agentcore/runtimes/Tokenomics{Support,Advisor}Agent-
#   3. The local infra/.deploy-region file.
#
# What it never touches:
#   - The CDKToolkit bootstrap stack, its S3 bucket and ECR repository. They are
#     shared account infrastructure that other CDK apps may use.
#   - Any stack or log group not named above.
#
# Stacks are deleted directly through CloudFormation (no CDK synth), so this runs
# without the Python venv, Node.js or a container runtime.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/common/common.sh"   # resolve_region

# Reverse dependency order: ControlPlane references AppPlane + Data outputs,
# AppPlane references Data. CloudFormation refuses to delete a stack whose
# exports are still imported, so the order matters.
STACKS="Tokenomics-ControlPlane Tokenomics-AppPlane Tokenomics-Data"
LOG_GROUP_PREFIXES="/aws/lambda/Tokenomics-Data- /aws/lambda/Tokenomics-AppPlane- /aws/lambda/Tokenomics-ControlPlane- /aws/bedrock-agentcore/runtimes/TokenomicsSupportAgent- /aws/bedrock-agentcore/runtimes/TokenomicsAdvisorAgent-"

ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help)
      echo "Usage: bash scripts/destroy-all.sh [--yes]"
      echo "  Deletes the Tokenomics stacks and their runtime log groups in the deploy region."
      echo "  Region: AWS_REGION, else infra/.deploy-region (written by deploy-all.sh)."
      exit 0
      ;;
    *) echo "[destroy-all] unknown argument: $arg (see --help)" >&2; exit 2 ;;
  esac
done

info () { echo "[destroy-all] $*"; }
fail () { echo "[destroy-all] ERROR: $*" >&2; exit 1; }

command -v aws >/dev/null 2>&1 || fail "AWS CLI v2 not found."
ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
[ -n "$ACCOUNT" ] && [ "$ACCOUNT" != "None" ] || fail "AWS CLI is not authenticated. Run 'aws configure' (or set AWS_PROFILE)."
resolve_region   # -> REGION (AWS_REGION > infra/.deploy-region > default)
if [ -n "${AWS_REGION:-}" ]; then
  REGION_SOURCE="AWS_REGION environment variable"
elif [ -f "$ROOT/infra/.deploy-region" ]; then
  REGION_SOURCE="infra/.deploy-region (saved by deploy-all.sh)"
else
  REGION_SOURCE="default; set AWS_REGION to choose another region"
fi

stack_status () {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" \
    --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "NONE"
}

# Log groups whose name starts with one of the exact Tokenomics prefixes.
list_log_groups () {
  local prefix
  for prefix in $LOG_GROUP_PREFIXES; do
    aws logs describe-log-groups --region "$REGION" --log-group-name-prefix "$prefix" \
      --query "logGroups[].logGroupName" --output text 2>/dev/null | tr '\t' '\n'
  done | sed '/^$/d;/^None$/d'
}

# --- 1. Show exactly what will be deleted ----------------------------------
LINE="=================================================================="
echo "$LINE"
echo " Tokenomics destroy"
echo "   Account: $ACCOUNT"
echo "   Region:  $REGION   ($REGION_SOURCE)"
echo "$LINE"
echo
info "Stacks in $REGION:"
FOUND=""
for s in $STACKS; do
  st="$(stack_status "$s")"
  if [ "$st" = "NONE" ]; then
    echo "    $s  (not found, skipped)"
  else
    echo "    $s  ($st)"
    FOUND="$FOUND $s"
  fi
done
LOG_GROUPS="$(list_log_groups)"
LOG_COUNT="$(printf '%s' "$LOG_GROUPS" | sed '/^$/d' | wc -l | tr -d ' ')"
info "Runtime log groups in $REGION: $LOG_COUNT"
echo
info "Not touched: CDKToolkit (shared bootstrap) and anything not named above."

if [ -z "$FOUND" ] && [ "$LOG_COUNT" = "0" ]; then
  info "Nothing to delete in $REGION."
  rm -f "$ROOT/infra/.deploy-region"
  exit 0
fi

# --- 2. Confirm (this cannot be undone) --------------------------------------
if [ "$ASSUME_YES" -ne 1 ]; then
  [ -t 0 ] || fail "no terminal to confirm from. Re-run with --yes to delete without a prompt."
  echo
  printf "[destroy-all] This permanently deletes the resources above in %s (account %s).\n" "$REGION" "$ACCOUNT"
  printf "[destroy-all] Type the region name (%s) to confirm, or anything else to cancel: " "$REGION"
  read -r ANSWER || true
  [ "$ANSWER" = "$REGION" ] || fail "confirmation did not match; nothing was deleted."
fi

# --- 3. Delete the stacks, one at a time, in dependency order ---------------
for s in $FOUND; do
  info "deleting $s ..."
  aws cloudformation delete-stack --region "$REGION" --stack-name "$s"
  if ! aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$s"; then
    echo >&2
    echo "[destroy-all] ERROR: $s did not delete cleanly. Resources that failed:" >&2
    aws cloudformation describe-stack-events --region "$REGION" --stack-name "$s" \
      --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
      --output text >&2 || true
    fail "fix the resource above (or delete it in the console), then re-run: bash scripts/destroy-all.sh"
  fi
  info "$s deleted."
done

# --- 4. Delete the runtime log groups (re-listed after the stacks are gone) --
LOG_GROUPS="$(list_log_groups)"
if [ -n "$LOG_GROUPS" ]; then
  info "deleting runtime log groups ..."
  printf '%s\n' "$LOG_GROUPS" | while IFS= read -r lg; do
    [ -n "$lg" ] || continue
    aws logs delete-log-group --region "$REGION" --log-group-name "$lg" \
      && echo "    deleted $lg" \
      || echo "    WARNING: could not delete $lg" >&2
  done
fi

# --- 5. Local state ----------------------------------------------------------
rm -f "$ROOT/infra/.deploy-region"

echo
info "Done. All Tokenomics resources in $REGION are removed."
info "The CDKToolkit bootstrap stack was kept (shared with other CDK apps)."
