# Tokenomics Advisor agent container for Amazon Bedrock AgentCore Runtime.
#
# Mirrors src/app-plane/Dockerfile: linux/arm64, BedrockAgentCoreApp serves
# /invocations (POST) + /ping (GET) on port 8080. The build context is the repo
# root, narrowed by CDK to _ADVISOR_IMAGE_FILES (infra/stacks/control_plane_stack.py):
# the agent, the read-only data-tool modules it runs in-process, src/shared and the
# pattern catalog. The app-plane image and src/.dockerignore are untouched.

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/control-plane \
    # Redact prompt/response/tool payloads from Strands OTel spans (keeps timing +
    # token usage); conversation text and tool results stay out of traces.
    OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_unredacted_attributes=

WORKDIR /app

COPY src/control-plane/advisor/agent/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

COPY src/control-plane/config.py /app/control-plane/config.py
COPY src/control-plane/lib /app/control-plane/lib
COPY src/control-plane/handlers /app/control-plane/handlers
COPY src/control-plane/advisor /app/control-plane/advisor
COPY src/shared /app/control-plane/shared
# The default config.ADVISOR_CATALOG_FILE location, so no env var is needed.
COPY seed/data/advisor/tokenomics-patterns-kb.md /app/control-plane/advisor/tokenomics-patterns-kb.md

# Run as an unprivileged user (port 8080 needs no root).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin advisor
USER 10001

EXPOSE 8080

# Liveness: BedrockAgentCoreApp serves GET /ping.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=2).status == 200 else 1)"]

CMD ["opentelemetry-instrument", "python", "-m", "advisor.agent.entrypoint"]
