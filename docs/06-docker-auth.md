# 06 — Docker & Authentication

The container is the security boundary ([ADR-005](00-decisions.md)) and the portability story.

---

## 1. One-time host setup

Run on a machine with a browser, signed in to the Max 20x account:

```bash
claude setup-token
```

This opens the browser authorization flow and prints an `sk-ant-oat01-...` token. Properties:

- Valid **one year**. No auto-refresh.
- Requires an active Pro/Max/Team/Enterprise plan.
- Authenticates against the **subscription**.
- **Not saved anywhere by the command** — capture it from stdout at that moment or re-run.
- Rejected by the Messages API. It only works with Claude Code.
- Cannot fetch claude.ai connectors, and cannot establish Remote Control sessions. Locally-configured
  MCP servers still work, so declare any server the child session needs via `mcp_servers`.

Store it as a Docker secret or in your secret manager. Never bake it into an image layer, and never
commit it.

---

## 2. Dockerfile

```dockerfile
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
    BROKER_CLI_PATH=/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js \
    CLAUDE_CONFIG_DIR=/var/lib/claude-broker/claude

USER broker
WORKDIR /workspace
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["/opt/venv/bin/python", "-m", "broker.healthcheck"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/venv/bin/python", "-m", "broker"]
```

**Why each choice**

- **`tini`.** The `claude` CLI spawns Node subprocesses which spawn shells. Without PID-1 reaping, a
  long-running broker accumulates zombies until it exhausts the process table.
- **Node installed explicitly, CLI pinned.** The SDK bundles a CLI, but its version moves with SDK
  releases and several behaviours are version-gated ([ADR-002](00-decisions.md)). `cli_path` points at
  the pinned install so there is no ambiguity about which binary runs.
- **`CLAUDE_CONFIG_DIR` under the state volume.** On Linux the CLI writes `.credentials.json` and
  onboarding state under the config dir. Redirecting it onto the persistent volume keeps a read-only
  root filesystem viable.
- **UID 10001, non-root.** Match it to the owner of your bind-mounted workspace or every write fails.
- **`git` installed.** Almost every real task needs it, and installing it at runtime would need network
  and root.

---

## 3. Startup preflight

`broker.config.preflight()` runs before binding the port. Any failure is fatal with a specific message —
these are the checks that prevent silent, expensive misconfiguration.

```python
def preflight(cfg: Config) -> None:
    # 1. Billing guard — see 00 §1.3. ANTHROPIC_API_KEY outranks CLAUDE_CODE_OAUTH_TOKEN.
    hijackers = [v for v in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    ) if os.environ.get(v)]
    if hijackers and not cfg.allow_api_billing:
        raise ConfigError(
            f"{', '.join(hijackers)} set and would take precedence over "
            f"CLAUDE_CODE_OAUTH_TOKEN, silently switching to API billing. "
            f"Unset it, or set BROKER_ALLOW_API_BILLING=1 to accept API charges."
        )

    # 2. Credential present
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and not cfg.allow_api_billing:
        raise ConfigError("CLAUDE_CODE_OAUTH_TOKEN is required. Run `claude setup-token` on a "
                          "machine with a browser and pass the result in.")

    # 3. CLI present and new enough
    version = probe_cli_version(cfg.cli_path)          # `node cli.js --version`
    if version < Version("2.1.219"):
        raise ConfigError(f"claude CLI {version} < required 2.1.219 (see ADR-002).")

    # 4. --bare must never be reachable: it does not read CLAUDE_CODE_OAUTH_TOKEN.
    if "bare" in cfg.passthrough_extra_args:
        raise ConfigError("--bare disables OAuth token reading and is incompatible with "
                          "subscription auth. Use setting_sources=[] for isolation instead.")

    # 5. Writable state, existing workspace roots
    assert_writable(cfg.state_dir)
    for root in cfg.workspace_roots:
        assert_directory(root)

    # 6. Transport auth
    if cfg.transport == "http" and cfg.host not in ("127.0.0.1", "localhost") \
            and not cfg.auth_token:
        raise ConfigError("BROKER_AUTH_TOKEN is required when binding to a non-loopback address.")

    # 7. Liveness probe — one trivial call, confirms the credential actually works
    probe_auth(cfg)     # `claude -p "ok" --output-format json --max-turns 1`
```

Check 7 costs a few cents and catches an expired token at boot instead of on the first real task. Worth
it. It also populates `broker_status.auth`.

---

## 4. Compose

```yaml
services:
  claude-broker:
    build:
      context: .
      args: {CLAUDE_CLI_VERSION: "2.1.219"}
    image: claude-broker:1.0.0
    container_name: claude-broker
    restart: unless-stopped

    environment:
      BROKER_TRANSPORT: http
      BROKER_HOST: 0.0.0.0
      BROKER_PORT: "8787"
      BROKER_DEFAULT_POLICY: reviewed
      BROKER_MAX_SESSIONS: "8"
      BROKER_DEFAULT_BUDGET_USD: "2.00"
      BROKER_GLOBAL_BUDGET_USD: "50.00"
      BROKER_LOG_LEVEL: INFO
      # Belt and braces: blank these so a host value cannot leak in and hijack precedence.
      ANTHROPIC_API_KEY: ""
      ANTHROPIC_AUTH_TOKEN: ""

    secrets: [claude_oauth_token, broker_auth_token]

    ports: ["127.0.0.1:8787:8787"]   # loopback only; front with a reverse proxy for remote access

    volumes:
      - type: bind
        source: ${WORKSPACE_DIR:-./workspace}
        target: /workspace
      - claude-broker-state:/var/lib/claude-broker

    read_only: true
    tmpfs: ["/tmp:size=512m,mode=1777"]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    pids_limit: 512
    mem_limit: 6g

secrets:
  claude_oauth_token:
    file: ./secrets/claude_oauth_token
  broker_auth_token:
    file: ./secrets/broker_auth_token

volumes:
  claude-broker-state:
```

The entrypoint reads `/run/secrets/claude_oauth_token` into `CLAUDE_CODE_OAUTH_TOKEN` at start.
Secrets-as-files beat secrets-as-env: environment variables leak into `docker inspect`, crash dumps,
and child process listings.

`mem_limit: 6g` assumes 8 concurrent sessions. Each live session is a Node process holding a model
context; budget roughly 600–700 MB per session plus overhead, and raise the limit if you raise
`BROKER_MAX_SESSIONS`.

**Windows host note.** The developer's host is Windows 11 with Docker Desktop. Bind-mounting a Windows
path into `/workspace` goes through a translation layer with two consequences worth planning around:
file-watching is unreliable, and I/O is slow enough to matter on large repos. For heavy work, clone
into a named volume instead of bind-mounting from `C:\`.

---

## 5. Transport security

- `Authorization: Bearer <BROKER_AUTH_TOKEN>` required on every request when not on loopback.
  Compare with `hmac.compare_digest`, never `==`.
- Bind to `127.0.0.1` on the host and put TLS in a reverse proxy for remote access. The broker does
  not terminate TLS itself.
- Rate-limit unauthenticated requests to 10/min/IP to blunt token guessing.
- **Never log the bearer token, the OAuth token, or full tool inputs at `INFO`.** Tool inputs can carry
  file contents and secrets; log the digest and the preview.

## 6. Egress

Under the `autonomous` policy the child agent can issue arbitrary network calls via `Bash` and
`WebFetch`. The container is what bounds that.

Minimum required egress:

| Destination | Why |
|---|---|
| `api.anthropic.com:443` | Model requests |
| DNS | Resolution |
| Package registries | Only if tasks install dependencies |
| Your git host | Only if tasks push |

For `autonomous`, attach the broker to a network whose egress is restricted to that list. A permission
policy cannot stop exfiltration by a tool call it has already allowed; a firewall can.
