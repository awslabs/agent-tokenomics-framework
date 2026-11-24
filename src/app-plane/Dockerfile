# Support agent container for Amazon Bedrock AgentCore Runtime.
#
# AgentCore Runtime requirements (verified against AWS/Strands docs):
#   - Platform MUST be linux/arm64
#   - App listens on port 8080 and exposes /invocations (POST) + /ping (GET)
#     (provided by BedrockAgentCoreApp)
#   - Image is pushed to ECR and referenced by the AgentCore Runtime
#
# Build/push is handled by scripts/deploy-app-plane.sh (AgentCore CLI) or a
# manual `docker buildx --platform linux/arm64 ... --push` to ECR.

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # app-plane code imports `config`, `store`, `tokenomics.*`, `agents.*`
    # (rooted at /app/app-plane) and `shared.*` (rooted at /app).
    PYTHONPATH=/app:/app/app-plane

WORKDIR /app

# Install dependencies first for better layer caching.
COPY app-plane/requirements.txt /app/app-plane/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/app-plane/requirements.txt

# Application code: the app plane plus the shared module it depends on.
COPY app-plane /app/app-plane
COPY shared /app/shared

WORKDIR /app/app-plane

EXPOSE 8080

# opentelemetry-instrument wires ADOT auto-instrumentation for AgentCore
# observability; the entrypoint boots BedrockAgentCoreApp (/invocations + /ping).
CMD ["opentelemetry-instrument", "python", "-m", "entrypoint"]
