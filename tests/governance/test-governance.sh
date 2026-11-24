#!/usr/bin/env bash
#
# Run the LIVE token-governance test suite against the DEPLOYED stack.
#
# Usage (from anywhere):
#   bash tests/governance/test-governance.sh               # everything (enforcement + analytics)
#   bash tests/governance/test-governance.sh analytics     # read-only calculation checks (FREE, no chat turns)
#   bash tests/governance/test-governance.sh enforcement   # the 8 settings fire (sends ~8 short chat turns; small Bedrock cost)
#
# Prerequisites:
#   * the stack is deployed (control plane + app plane),
#   * AWS credentials for that account/region are in your environment
#     (the same creds the `aws` CLI uses),
#   * the app-plane venv exists (created by scripts/deploy-all.sh).
#
# Env:
#   TOKENOMICS_REGION          (optional, default us-west-2)
#   TOKENOMICS_USER_PASSWORD   (required)  — demo chat users (bob/eva)
#   TOKENOMICS_ADMIN_PASSWORD  (required)  — control-plane admin
#   (printed by the seed on first deploy, or the values you set at deploy time)
#
set -euo pipefail

# Repo root is two levels up from this script (tests/governance/test-governance.sh).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PY="src/app-plane/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "ERROR: app-plane venv python not found at $PY" >&2
  echo "       create it first (see scripts/deploy-all.sh)." >&2
  exit 1
fi

# Fail fast if there are no AWS creds — every live test needs them.
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "ERROR: no usable AWS credentials in this shell." >&2
  echo "       configure them (aws configure / SSO / env vars) and retry." >&2
  exit 1
fi

MODE="${1:-all}"
case "$MODE" in
  analytics)
    TARGET="tests/governance/test_analytics.py"
    echo "[governance-tests] running ANALYTICS checks (read-only, no chat turns)…"
    ;;
  enforcement)
    TARGET="tests/governance/test_enforcement.py"
    echo "[governance-tests] running ENFORCEMENT checks — sends ~8 short chat turns (small Bedrock cost)…"
    ;;
  all)
    TARGET="tests/governance"
    echo "[governance-tests] running ALL governance live tests (enforcement sends ~8 short chat turns; small Bedrock cost)…"
    ;;
  -h|--help|help)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "ERROR: unknown mode '$MODE' (use: analytics | enforcement | all)" >&2
    exit 2
    ;;
esac

# `-m live` on the CLI overrides the default `-m "not live"` in pytest.ini.
exec "$PY" -m pytest "$TARGET" -m live -v
