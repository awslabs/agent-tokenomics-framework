"""Shared Cognito hardening for both user pools (app plane + control plane).

One place for the sign-in controls so the customer and admin pools can't drift:
  - password policy: 12+ chars with upper, lower, digit and symbol;
  - optional threat protection (compromised-credential detection, adaptive
    authentication / risk-based blocking). It needs the PLUS feature plan, which
    is billed per MAU, so it is OFF by default. Enable it per environment with
    `"cognitoThreatProtection": true` in infra/cdk.json context (the deploy
    scripts pick that up) or `cdk deploy ... -c cognitoThreatProtection=true`.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import aws_cognito as cognito
from constructs import Construct

_THREAT_PROTECTION_CTX = "cognitoThreatProtection"


def password_policy() -> cognito.PasswordPolicy:
    return cognito.PasswordPolicy(
        min_length=12,
        require_lowercase=True,
        require_uppercase=True,
        require_digits=True,
        require_symbols=True,
    )


def threat_protection_props(scope: Construct) -> dict[str, Any]:
    """UserPool kwargs that enable full-function threat protection, if opted in.

    Returns {} when the context flag is off, leaving the pool's feature plan at
    the account default (no PLUS billing).
    """
    raw = scope.node.try_get_context(_THREAT_PROTECTION_CTX)
    if str(raw).strip().lower() not in ("true", "1", "yes"):
        return {}
    return {
        "feature_plan": cognito.FeaturePlan.PLUS,
        "standard_threat_protection_mode": cognito.StandardThreatProtectionMode.FULL_FUNCTION,
    }


def spa_auth_flows() -> cognito.AuthFlow:
    """SPA client flows: SRP only for browsers (no plaintext USER_PASSWORD_AUTH).

    ADMIN_USER_PASSWORD_AUTH stays on for the live test suites; it requires IAM
    credentials (cognito-idp:AdminInitiateAuth), so it is not reachable from the
    public client.
    """
    return cognito.AuthFlow(user_srp=True, admin_user_password=True)
