"""API Gateway stage with JSON access logs and throttling, shared by both APIs.

Threat model M16 (T11): one access-log line per request, including the caller's
Cognito sub and email, so abusive traffic and config changes can be attributed.
Threat model M12 (T6): a stage-wide request rate / burst limit, so a flood of
requests is rejected with 429 before it reaches the Lambda or Bedrock. (Per-user
token spend is capped separately, in-loop, by the agent's QuotaHook.)

API Gateway's account-level CloudWatch role is already created by each
LambdaRestApi (cloud_watch_role defaults on), which access logging requires.
"""

from __future__ import annotations

import json

from aws_cdk import RemovalPolicy
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_logs as logs
from constructs import Construct

F = apigw.AccessLogField


def stage_options(scope: Construct, cid: str, *, rate_limit: int, burst_limit: int) -> apigw.StageOptions:
    """Stage `v1`: access logs to a 1-month log group named `cid`, plus throttling
    (`rate_limit` requests/second steady state, `burst_limit` concurrent burst)."""
    log_group = logs.LogGroup(
        scope,
        cid,
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,  # demo resource
    )
    fmt = {
        "requestId": F.context_request_id(),
        "requestTime": F.context_request_time(),
        "ip": F.context_identity_source_ip(),
        "userAgent": F.context_identity_user_agent(),
        "httpMethod": F.context_http_method(),
        # The real request path (e.g. /v1/flags); resourcePath is just /{proxy+}.
        "path": F.context_path(),
        "resourcePath": F.context_resource_path(),
        "status": F.context_status(),
        "responseLength": F.context_response_length(),
        "latencyMs": F.context_response_latency(),
        "sub": F.context_authorizer_claims("sub"),
        "email": F.context_authorizer_claims("email"),
    }
    return apigw.StageOptions(
        stage_name="v1",
        access_log_destination=apigw.LogGroupLogDestination(log_group),
        access_log_format=apigw.AccessLogFormat.custom(json.dumps(fmt)),
        throttling_rate_limit=rate_limit,
        throttling_burst_limit=burst_limit,
    )
