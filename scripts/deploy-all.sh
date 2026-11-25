#!/usr/bin/env bash
#
# One-shot full deploy for the Agent Tokenomics Framework -- run this and forget.
#
# This is the single entry point a new user runs to stand up the whole solution:
# it sets up the local environment, then deploys BOTH planes end to end.
#
#   bash scripts/deploy-all.sh
#     -> environment setup (this script)
#     -> scripts/deploy-app-plane.sh   (Data + AppPlane stacks + chat UI + seed)
#     -> scripts/deploy-control.sh     (ControlPlane stack + admin UI + seed)
#
# To run a stage on its own instead, run deploy-app-plane.sh / deploy-control.sh
# directly -- they reuse the region this script saved to infra/.deploy-region.
#
# Environment setup phase (below) does NOT deploy; it only prepares the host:
#   1. Preflight: verify the required tools exist and meet minimum versions.
#      (It checks and instructs -- it never auto-installs system tools.)
#   2. Region: pick one of the supported Regions (us-east-1, us-east-2,
#      us-west-2) from a numbered menu (default us-east-1) and persist it to
#      infra/.deploy-region so the deploy scripts reuse it.
#   3. CDK bootstrap health: detect a CDKToolkit stack left in a failed state
#      and offer the non-destructive recovery (never auto-deletes shared infra).
#   4. Python: create the CDK virtualenv at infra/.venv and install
#      infra/requirements.txt into it (idempotent).
#   5. SPAs: npm install for web/control-plane and web/app-plane.
#   Also (right after the region): prompt for the demo login passwords -- one
#   for the Admin UI user, one shared by the Chat UI users -- checked against
#   the Cognito policy. They are kept in memory for this run only; the deploy
#   scripts create the Cognito users with them before seeding.
# Then it runs the two deploy scripts in order and prints the UI URLs + logins.
#
# Usage:
#   bash scripts/deploy-all.sh                         # prompts for region + passwords
#   AWS_REGION=us-east-1 bash scripts/deploy-all.sh    # skip the region prompt
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/common/common.sh"   # password_ok, PASSWORD_POLICY_TEXT, cfn_output
PYBIN="$ROOT/infra/.venv/bin/python"
REGION_FILE="$ROOT/infra/.deploy-region"
USERS_JSON="$ROOT/seed/data/users.json"
ADMIN_EMAIL="admin@example.com"

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      echo "Usage: bash scripts/deploy-all.sh"
      echo "  Full deploy: environment setup, then the app-plane + control-plane deploys."
      echo "  Prompts for the region and the demo login passwords (Admin UI + Chat UI)."
      echo "  Set AWS_REGION to skip the region prompt."
      exit 0
      ;;
    *) echo "[deploy-all] unknown argument: $arg (see --help)" >&2; exit 2 ;;
  esac
done

info () { echo "[deploy-all] $*"; }
fail () { echo "[deploy-all] ERROR: $*" >&2; exit 1; }

# --- 1. Preflight: required tools + minimum versions ------------------------
# Compare two dotted versions: returns 0 if $1 >= $2.
version_ge () {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -t. -k1,1n -k2,2n -k3,3n | head -n1)" = "$2" ]
}

info "checking prerequisites..."

command -v aws >/dev/null 2>&1 || fail "AWS CLI v2 not found. Install: https://aws.amazon.com/cli/"

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not found. Install Python 3.12+: https://www.python.org/downloads/"
fi
PY_VER="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
version_ge "$PY_VER" "3.12.0" || fail "Python $PY_VER is too old; need 3.12+. (Tip: 'python3 --version'.)"
info "python3 $PY_VER OK"

if ! command -v node >/dev/null 2>&1; then
  fail "Node.js not found. Install Node 20.19+ or 22.12+: https://nodejs.org/"
fi
NODE_VER="$(node -v | sed 's/^v//')"
# Vite 8 requires ^20.19.0 || >=22.12.0. Accept 20.19+ or 22.12+ (or newer major).
NODE_MAJOR="${NODE_VER%%.*}"
if [ "$NODE_MAJOR" -eq 20 ]; then
  version_ge "$NODE_VER" "20.19.0" || fail "Node $NODE_VER too old; need 20.19+ (or 22.12+)."
elif [ "$NODE_MAJOR" -eq 21 ]; then
  fail "Node $NODE_VER unsupported by Vite 8; use 20.19+ or 22.12+."
elif [ "$NODE_MAJOR" -eq 22 ]; then
  version_ge "$NODE_VER" "22.12.0" || fail "Node $NODE_VER too old; need 22.12+ (or 20.19+)."
elif [ "$NODE_MAJOR" -lt 20 ]; then
  fail "Node $NODE_VER too old; need 20.19+ or 22.12+."
fi
info "node $NODE_VER OK"

command -v npm >/dev/null 2>&1 || fail "npm not found (comes with Node.js)."

if ! docker info >/dev/null 2>&1; then
  fail "Docker daemon not reachable. Start Docker/Finch/Podman (needed to build the ARM64 agent image)."
fi
info "docker OK"

# AWS credentials must resolve to an account.
ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
[ -n "$ACCOUNT" ] && [ "$ACCOUNT" != "None" ] || fail "AWS CLI is not authenticated. Run 'aws configure' (or set AWS_PROFILE)."
info "AWS account $ACCOUNT OK"

# --- 2. Region --------------------------------------------------------------
# Only the supported Regions (SUPPORTED_REGIONS in scripts/common/common.sh).
# Honor a pre-set AWS_REGION (must be supported); otherwise show a numbered menu.
# us-east-1 is the default: it matches the CDK app default (infra/app.py) and
# the agent runtime default (src/*/config.py).
if [ -n "${AWS_REGION:-}" ]; then
  REGION="$AWS_REGION"
  region_supported "$REGION" \
    || fail "'$REGION' is not supported. Supported: ${SUPPORTED_REGIONS// /, }."
  info "using AWS_REGION from environment: $REGION"
elif [ ! -t 0 ]; then
  REGION="$DEFAULT_REGION"
  info "no terminal to prompt; using the default region $REGION (set AWS_REGION to choose)."
else
  info "Select the AWS Region to deploy into:"
  i=0
  for r in $SUPPORTED_REGIONS; do
    i=$((i + 1))
    if [ "$r" = "$DEFAULT_REGION" ]; then tag="   [default]"; else tag=""; fi
    printf "  %d) %-11s %s%s\n" "$i" "$r" "$(region_label "$r")" "$tag"
  done
  REGION=""
  tries=0
  while [ -z "$REGION" ]; do
    tries=$((tries + 1))
    [ "$tries" -le 3 ] || fail "no valid region selected. Re-run: bash scripts/deploy-all.sh"
    printf "[setup] Enter 1-%d (press Enter for 1): " "$i"
    read -r CHOICE || true
    # Strip all whitespace and stray backslashes that some terminals inject.
    CHOICE="$(printf '%s' "$CHOICE" | tr -d '[:space:]\\')"
    CHOICE="${CHOICE:-1}"
    case "$CHOICE" in
      *[!0-9]*) ;;
      *) [ "$CHOICE" -ge 1 ] && [ "$CHOICE" -le "$i" ] && REGION="$(echo $SUPPORTED_REGIONS | cut -d' ' -f"$CHOICE")" ;;
    esac
    [ -n "$REGION" ] || info "Please enter a number from 1 to $i."
  done
fi
# Persist so the deploy scripts (steps 2 and 3) reuse it without re-asking.
printf '%s\n' "$REGION" > "$REGION_FILE"
info "region: $REGION (saved to infra/.deploy-region)"
info "REMINDER: enable Bedrock model access (Claude Sonnet 5.5, Claude Haiku 5.5, Amazon Nova Lite) in $REGION."
info "          The Bedrock console step cannot be automated; the agent fails at invoke time without it."

# --- 2b. Demo login passwords ----------------------------------------------
# Self sign-up is off, so the deploy creates the UI users. Ask for their
# passwords up front (hidden, confirmed, checked against the Cognito policy) so
# the deploy never fails on a bad password at the end. Kept in memory only and
# exported for the deploy scripts; never written to disk or printed.
CHAT_EMAILS="$(python3 -c '
import json, sys
e = [u["email"] for u in json.load(open(sys.argv[1]))]
print(" and ".join([", ".join(e[:-1]), e[-1]]) if len(e) > 1 else e[0])' "$USERS_JSON")"

# ask_password <var> <prompt> <confirm-prompt> <label>
ask_password () {
  local var="$1" prompt="$2" confirm="$3" label="$4" pw pw2 tries=0
  if [ -n "${!var:-}" ]; then
    password_ok "${!var}" || fail "the preset $label password does not meet the policy: $PASSWORD_POLICY_TEXT."
    info "$label password: using preset value"
    export "${var?}"
    return 0
  fi
  [ -t 0 ] || fail "no terminal to read the $label password from. Run ./scripts/deploy-all.sh in an interactive terminal."
  while :; do
    tries=$((tries + 1))
    [ "$tries" -le 3 ] || fail "too many attempts for the $label password. Re-run: ./scripts/deploy-all.sh"
    IFS= read -r -s -p "[setup] $prompt: " pw; echo
    if ! password_ok "$pw"; then
      info "password must be $PASSWORD_POLICY_TEXT. Try again."
      continue
    fi
    IFS= read -r -s -p "[setup] $confirm: " pw2; echo
    if [ "$pw" != "$pw2" ]; then
      info "passwords do not match. Try again."
      continue
    fi
    break
  done
  printf -v "$var" '%s' "$pw"
  export "${var?}"
  info "$label password OK"
}

echo
info "Demo login passwords (needed to sign in to the UIs; not stored anywhere)."
info "  Rules: $PASSWORD_POLICY_TEXT (e.g. ! @ # \$ % ^ & *)."
echo
ask_password TOKENOMICS_ADMIN_PASSWORD "Admin UI password for $ADMIN_EMAIL" "Confirm admin password" "admin"
echo
ask_password TOKENOMICS_USER_PASSWORD "Chat UI password for $CHAT_EMAILS" "Confirm chat password" "chat"
echo

# --- 3. CDK bootstrap health check ------------------------------------------
# CDK bootstraps a shared "CDKToolkit" CloudFormation stack per account/region.
# If a PREVIOUS bootstrap left it in a failed state, every deploy here fails with
# a cryptic error. Detect that early. We only ever offer the NON-destructive
# `continue-update-rollback` recovery, and only with confirmation -- we never
# delete the stack (it is shared account infra that other CDK apps may use).
BOOT_STATUS="$(aws cloudformation describe-stacks --region "$REGION" \
  --stack-name CDKToolkit --query "Stacks[0].StackStatus" --output text 2>/dev/null || true)"

case "$BOOT_STATUS" in
  ""|None)
    info "CDK bootstrap: not yet bootstrapped in $REGION (the deploy will bootstrap it)."
    ;;
  UPDATE_ROLLBACK_FAILED)
    # Recoverable, non-destructively, via continue-update-rollback (with consent).
    echo
    info "WARNING: the CDK bootstrap stack (CDKToolkit) in $REGION is in state: $BOOT_STATUS"
    info "This is a pre-existing account/region issue (not this project) and will block deploys."
    printf "[setup] Attempt the non-destructive recovery (continue-update-rollback) now? [y/N]: "
    read -r ANSWER || true
    case "$ANSWER" in
      y|Y|yes|YES)
        info "running: aws cloudformation continue-update-rollback --stack-name CDKToolkit --region $REGION"
        aws cloudformation continue-update-rollback --stack-name CDKToolkit --region "$REGION" \
          || fail "continue-update-rollback failed. Inspect the stack in the CloudFormation console; a specific resource may need --resources-to-skip."
        info "waiting for CDKToolkit to reach a stable state..."
        aws cloudformation wait stack-update-complete --stack-name CDKToolkit --region "$REGION" \
          || fail "CDKToolkit did not recover. Check the CloudFormation console for the blocking resource."
        info "CDKToolkit recovered. Continuing setup."
        ;;
      *)
        fail "Skipped. Recover CDKToolkit before deploying:
    aws cloudformation continue-update-rollback --stack-name CDKToolkit --region $REGION"
        ;;
    esac
    ;;
  ROLLBACK_COMPLETE|ROLLBACK_FAILED|CREATE_FAILED|DELETE_FAILED)
    # These require delete + re-bootstrap, which is destructive on shared infra --
    # we do NOT do that automatically.
    echo
    info "WARNING: the CDK bootstrap stack (CDKToolkit) in $REGION is in state: $BOOT_STATUS"
    info "This is a pre-existing account/region issue (not this project) and will block deploys."
    fail "CDKToolkit in $REGION is $BOOT_STATUS and must be recreated manually before deploying.
  If no other CDK app in this account/region depends on it, delete and let the deploy re-bootstrap:
    aws cloudformation delete-stack --stack-name CDKToolkit --region $REGION
    aws cloudformation wait stack-delete-complete --stack-name CDKToolkit --region $REGION
  Then re-run: bash scripts/deploy-all.sh"
    ;;
  *_IN_PROGRESS)
    fail "CDKToolkit in $REGION is $BOOT_STATUS (an operation is in progress). Wait for it to finish, then re-run: bash scripts/deploy-all.sh"
    ;;
  *)
    # Healthy stable states: CREATE_COMPLETE, UPDATE_COMPLETE, UPDATE_ROLLBACK_COMPLETE.
    info "CDK bootstrap: CDKToolkit is healthy ($BOOT_STATUS)."
    ;;
esac

# --- 4. Python virtualenv (infra/.venv) -------------------------------------
if [ -x "$PYBIN" ]; then
  info "infra/.venv already exists; ensuring requirements are installed..."
else
  info "creating infra/.venv with python3 ($PY_VER)..."
  python3 -m venv "$ROOT/infra/.venv"
fi
[ -x "$PYBIN" ] || fail "venv creation failed; infra/.venv/bin/python is missing."
"$PYBIN" -m pip install --upgrade pip -q
"$PYBIN" -m pip install -r "$ROOT/infra/requirements.txt"
info "CDK virtualenv ready: $("$PYBIN" --version)"

# --- 5. SPA dependencies ----------------------------------------------------
info "installing control-plane SPA dependencies..."
( cd "$ROOT/web/control-plane" && npm install )
info "installing app-plane SPA dependencies..."
( cd "$ROOT/web/app-plane" && npm install )

echo
info "Environment ready (region: $REGION). Deploying both planes..."
info "Make sure Bedrock model access is enabled in $REGION (Sonnet 5.5 / Haiku 5.5 / Nova Lite)."

# --- 6. Deploy both planes (app plane first; control plane cross-references it) ---
# Export the region so the deploy scripts don't re-prompt (they also read
# infra/.deploy-region, written above). set -e aborts the whole run if either fails.
export AWS_REGION="$REGION"

echo
info "=== Deploying the app plane ==="
bash "$ROOT/scripts/deploy-app-plane.sh"

echo
info "=== Deploying the control plane ==="
bash "$ROOT/scripts/deploy-control.sh"

CHAT_UI="$(cfn_output "$REGION" Tokenomics-AppPlane ChatUiUrl)"
ADMIN_UI="$(cfn_output "$REGION" Tokenomics-ControlPlane WebUrl)"
LINE="=================================================================="
echo
echo "$LINE"
echo " Deploy complete ($REGION)"
echo
echo " Chat UI   $CHAT_UI"
python3 -c '
import json, sys
for u in json.load(open(sys.argv[1])):
    print("           %-17s (%s tier)" % (u["email"], u.get("tier", "basic")))' "$USERS_JSON"
echo "           password: the chat password you entered"
echo
echo " Admin UI  $ADMIN_UI"
echo "           $ADMIN_EMAIL"
echo "           password: the admin password you entered"
echo
echo " Forgot a password? Re-run ./scripts/deploy-all.sh to set new ones."
echo "$LINE"
