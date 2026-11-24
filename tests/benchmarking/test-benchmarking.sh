#!/usr/bin/env bash
#
# Run the LIVE benchmarking test against the DEPLOYED stack.
#
# Starts a REAL multi-turn A/B benchmark (the same thing the Benchmarks page does
# on "Run"): 2 sessions x 3 turns, Run A = memory only, Run B = memory + prompt
# caching. The agent actually answers all 12 turns on AgentCore; the test polls to
# COMPLETED and prints the A-vs-B comparison (cost, cache, gain).
#
# Usage (from anywhere):
#   bash tests/benchmarking/test-benchmarking.sh
#
# Prerequisites:
#   * the stack is deployed (control plane + app plane; the agent reachable),
#   * AWS credentials for that account/region are in your environment,
#   * the app-plane venv exists (created by scripts/deploy-all.sh),
#   * Bedrock model access enabled in the region for the agent's models.
#
# COST: sends 12 short real agent turns (small Bedrock spend). The benchmark runs
# ungoverned and is tagged source=benchmark, so it never trips a quota or pollutes
# live analytics.
#
# Env:
#   TOKENOMICS_REGION          (optional, default us-west-2)
#   TOKENOMICS_ADMIN_PASSWORD  (required)  — control-plane admin password (the
#                              value set at deploy time)
#
set -euo pipefail

# Repo root is two levels up from this script (tests/benchmarking/test-benchmarking.sh).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PY="src/app-plane/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "ERROR: app-plane venv python not found at $PY" >&2
  echo "       create it first (see scripts/deploy-all.sh)." >&2
  exit 1
fi

# Fail fast if there are no AWS creds — the live test needs them.
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "ERROR: no usable AWS credentials in this shell." >&2
  echo "       configure them (aws configure / SSO / env vars) and retry." >&2
  exit 1
fi

echo "[benchmarking-tests] running a LIVE 2x3 A/B benchmark against the deployed stack"
echo "[benchmarking-tests]   Run A = memory | Run B = memory + prompt caching"
echo "[benchmarking-tests]   (sends 12 short real agent turns; small Bedrock cost)…"

# `-m live` overrides the default `-m "not live"` in pytest.ini; `-s` shows the
# printed A-vs-B comparison summary.
exec "$PY" -m pytest tests/benchmarking -m live -v -s
