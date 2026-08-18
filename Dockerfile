# syntax=docker/dockerfile:1.7

########## builder ##########
FROM python:3.12-slim AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir .

########## runtime ##########
FROM python:3.12-slim AS runtime

ARG CLAUDE_CLI_VERSION=2.1.219
ARG NODE_MAJOR=22

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git tini \
 && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}" \
 && npm cache clean --force \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin broker \
 && mkdir -p /workspace /var/lib/claude-broker \
 && chown -R broker:broker /workspace /var/lib/claude-broker

COPY --from=builder /opt/venv /opt/venv
COPY --chown=broker:broker policies/ /app/policies/

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BROKER_STATE_DIR=/var/lib/claude-broker \
    BROKER_WORKSPACE_ROOTS=/workspace \
    BROKER_POLICY_FILE=/app/policies/default.yaml \
    BROKER_CLI_PATH=/usr/bin/claude \
    CLAUDE_CONFIG_DIR=/var/lib/claude-broker/claude

USER broker
WORKDIR /workspace
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["/opt/venv/bin/python", "-m", "broker.healthcheck"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/venv/bin/python", "-m", "broker"]
