# claude-broker

A standalone MCP server that exposes a **long-lived, non-blocking Claude Code session broker** to any
MCP-capable LLM client. Runs in Docker. Authenticates against a Claude Max/Pro subscription, not the
Anthropic API.

> **Status: specification only.** No implementation code exists in this folder yet. These documents are
> implementer-grade: they specify exact tool schemas, state machines, file layout, and tests. An
> implementer should not need to make design decisions.

---

## Why this exists

The reference point for this project is [`steipete/claude-code-mcp`](https://github.com/steipete/claude-code-mcp),
which was **archived on 15 May 2026**. That server exposed a single one-shot `claude_code` tool that
shelled out to the CLI with `--dangerously-skip-permissions` and blocked until the run finished.

That design has three fatal properties for autonomous operation:

1. **It blocks.** A 20-minute agent run exceeds the request timeout of most MCP clients.
2. **It has no memory.** Every call is a cold start; there is no way to hold a conversation with a session.
3. **It has no brakes.** Permissions are bypassed wholesale, and nothing tracks spend or rate limits.

`claude-broker` is a clean-room design addressing all three. **No code is reused from the archived
project.**

## What it is not

This is not a way to smuggle subscription credentials into a third-party product. It drives the official
`claude` binary using your own login, which is the sanctioned path. See
[`docs/00-decisions.md`](docs/00-decisions.md) for the full terms-of-service reasoning.

---

## Document map

Read in order. Each builds on the last.

| # | Document | What it settles |
|---|---|---|
| 00 | [Decisions & constraints](docs/00-decisions.md) | Billing reality, ToS position, ADRs, verified facts |
| 01 | [Architecture](docs/01-architecture.md) | Components, processes, data flow, file layout |
| 02 | [MCP tool contract](docs/02-mcp-tool-contract.md) | Every tool, exact JSON schemas, errors |
| 03 | [Session broker](docs/03-session-broker.md) | Lifecycle state machine, event log, persistence |
| 04 | [Permission broker](docs/04-permission-broker.md) | Policy engine, `can_use_tool`, park-and-resolve |
| 05 | [Budget & rate limits](docs/05-budget-ratelimit.md) | Cost ledger, cooldown, backoff |
| 06 | [Docker & auth](docs/06-docker-auth.md) | Container, credentials, egress, hardening |
| 07 | [Testing](docs/07-testing.md) | Full test list with acceptance criteria |
| 08 | [Implementation plan](docs/08-implementation-plan.md) | Ordered, reviewable task breakdown |

---

## The shape of it, in one example

```jsonc
// 1. Create a session. Returns immediately.
{"tool": "session_create", "arguments": {
  "workspace": "/workspace/my-repo",
  "model": "opus",
  "permission_policy": "reviewed",
  "max_budget_usd": 5.00
}}
// -> {"session_id": "0f9c...", "state": "idle"}

// 2. Send work. Returns immediately with a turn handle.
{"tool": "session_send", "arguments": {
  "session_id": "0f9c...",
  "prompt": "Refactor the auth module and run the tests.",
  "wait_ms": 5000
}}
// -> {"turn_id": "t_01", "state": "running", "cursor": 12, "events": [...]}

// 3. Poll for progress. Cheap, incremental, resumable.
{"tool": "session_poll", "arguments": {"session_id": "0f9c...", "cursor": 12}}
// -> {"state": "awaiting_permission", "cursor": 31, "events": [...], "pending_permissions": [...]}

// 4. Approve the one thing that needed a human (or a policy).
{"tool": "permission_resolve", "arguments": {"request_id": "p_03", "decision": "allow"}}

// 5. Continue the conversation later. Context is intact.
{"tool": "session_send", "arguments": {"session_id": "0f9c...", "prompt": "Now update the changelog."}}
```

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Claude subscription | Pro, Max 5x, Max 20x, Team, or Enterprise | Required by `claude setup-token` |
| Docker | 28.x+ | Verified against 28.3.2 |
| Python | 3.12 | SDK requires 3.10+; container pins 3.12 |
| Node.js | 22 LTS | Runtime for the `claude` CLI, installed in the image |
| `claude-agent-sdk` | 0.2.129 | Pinned; see [ADR-002](docs/00-decisions.md) |

One-time host setup, run **interactively** on a machine with a browser:

```bash
claude setup-token
```

Copy the resulting `sk-ant-oat01-...` token into your secret store. It is valid for one year and is
never written to disk by the command itself.
