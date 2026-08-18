# Operator Guide

## Quickstart

### Local development (stdio)

```bash
pip install -e ".[dev]"
export CLAUDE_CODE_OAUTH_TOKEN=$(cat your-token.txt)
export BROKER_SKIP_AUTH_PROBE=1
export BROKER_TRANSPORT=stdio
export BROKER_WORKSPACE_ROOTS=$(pwd)/workspace
export BROKER_STATE_DIR=$(pwd)/.broker-state
mkdir -p workspace .broker-state
python -m broker
```

### Docker

```bash
mkdir -p secrets workspace
echo "your-oauth-token" > secrets/claude_oauth_token
echo "your-broker-auth-token" > secrets/broker_auth_token
docker compose up --build
```

Connect MCP clients to `http://127.0.0.1:8787` with `Authorization: Bearer <BROKER_AUTH_TOKEN>`.

## Policy authoring

Edit `policies/default.yaml`. Rules use Claude Code permission syntax:

- `Read` — match any Read call
- `Bash(git diff *)` — prefix match; **space before `*` is significant**
- `mcp__*` — all MCP tools

Put specific rules before general ones. The loader rejects shadowed deny rules.

Policies: `readonly`, `reviewed` (default), `autonomous`.

## Troubleshooting

### Expired token

Sessions fail with auth errors. Run `claude setup-token` on a machine with a browser, update the secret, restart the broker. Check `broker_status` for `days_remaining`.

### API key hijack

If `ANTHROPIC_API_KEY` is set in the environment, it **outranks** the OAuth token and switches to pay-as-you-go billing. The broker refuses to start unless `BROKER_ALLOW_API_BILLING=1`. Check `broker_status.auth.billing` — it must read `subscription`.

### Windows bind-mount performance

Bind-mounting `C:\` paths into Docker is slow and breaks file-watching. For heavy repos, use a Docker named volume instead.

## Token renewal runbook

`CLAUDE_CODE_OAUTH_TOKEN` expires after one year with no auto-refresh.

1. On a machine with a browser, run `claude setup-token`
2. Update `secrets/claude_oauth_token` or your secret manager
3. `docker compose restart claude-broker`
4. Verify via `broker_status`: `days_remaining` should be ~365
