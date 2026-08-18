# 00 — Decisions & Constraints

Everything in this document was verified against primary sources on **2026-08-04**. Where a claim
affects the design, the source is cited. Re-verify the billing section before any production rollout;
it is the one part with a known scheduled change.

---

## 1. Verified facts

### 1.1 Billing: subscription usage currently applies

The original premise for this project was that Anthropic had closed off subscription-backed
programmatic use. **That is not the current state.**

Anthropic announced that, from 15 June 2026, Agent SDK and `claude -p` usage would stop drawing on
subscription limits and instead consume a separate monthly credit pool at API rates (Pro $20 / Max 5x
$100 / Max 20x $200). That change was then **paused**. The support page currently reads:

> "We're pausing the changes to Claude Agent SDK usage described below. For now, nothing has changed:
> Claude Agent SDK... still draw from your subscription's usage limits."

**Consequence:** headless `claude -p` on a Max 20x plan bills against Max 20x limits today. No
workaround is needed.

**Risk:** the pause can be lifted without notice. The broker must therefore track `total_cost_usd`
from every `ResultMessage` and expose it (see [05](05-budget-ratelimit.md)). If Anthropic un-pauses,
the cost ledger will show it immediately rather than after a surprise invoice.

*Source: [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)*

### 1.2 Terms of service: this design is compliant

What Anthropic prohibits is using **subscription OAuth tokens inside third-party products** — i.e.
extracting a token and pointing a non-Anthropic client at the Messages API with it.

What this design does instead is drive the **official `claude` binary**, authenticated with the user's
own credential, on the user's own machine. That is the same mechanism Claude Code itself uses. The
`CLAUDE_CODE_OAUTH_TOKEN` produced by `claude setup-token` is documented by Anthropic explicitly for
"CI pipelines and scripts where browser login isn't available," and is hard-scoped: it can only make
model requests, and **the Messages API rejects it**.

The broker is a session manager for a first-party binary. It is not a client of the Anthropic API and
holds no API key.

*Source: [Authentication](https://code.claude.com/docs/en/authentication)*

### 1.3 Authentication precedence — the critical trap

Claude Code resolves credentials in this order:

| # | Credential | Billing |
|---|---|---|
| 1 | Cloud provider (`CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` / `_FOUNDRY`) | Cloud provider |
| 2 | `ANTHROPIC_AUTH_TOKEN` | Gateway / API |
| 3 | `ANTHROPIC_API_KEY` | **API, pay-as-you-go** |
| 4 | `apiKeyHelper` script output | API |
| 5 | `CLAUDE_CODE_OAUTH_TOKEN` | **Subscription** ← what we want |
| 6 | Interactive `/login` OAuth credentials | Subscription |

**`ANTHROPIC_API_KEY` outranks `CLAUDE_CODE_OAUTH_TOKEN`.** If it leaks into the container
environment — from a base image, a CI runner, a stray `.env`, a developer's shell — the broker
silently switches to API billing. In non-interactive `-p` mode there is no approval prompt to catch it.

**Mandatory mitigation:** the broker refuses to start if any of `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, or
`CLAUDE_CODE_USE_FOUNDRY` is set, unless `BROKER_ALLOW_API_BILLING=1` is explicitly passed. See
[06 §3](06-docker-auth.md).

### 1.4 `--bare` is unusable here

`--bare` skips discovery of hooks, skills, plugins, MCP servers, and `CLAUDE.md`, and is documented as
"the recommended mode for scripted and SDK calls." It is nonetheless **forbidden in this project**:

> "Bare mode does not read `CLAUDE_CODE_OAUTH_TOKEN`. If your script passes `--bare`, authenticate with
> `ANTHROPIC_API_KEY` or an `apiKeyHelper` instead."

Bare mode and subscription billing are mutually exclusive. We choose subscription billing.

Reproducibility is instead achieved with `setting_sources=[]` and `strict_mcp_config=True`, which
suppress host configuration **without** disabling OAuth credential reading.

### 1.5 Token capabilities and limits

`CLAUDE_CODE_OAUTH_TOKEN`:

- Valid for **one year**; no automatic refresh. When it expires, sessions fail hard.
- Requires an active Pro/Max/Team/Enterprise plan.
- **Can** use locally-configured MCP servers.
- **Cannot** establish Remote Control sessions.
- **Cannot** fetch claude.ai connectors. Any MCP server the child session needs must be declared
  locally via `mcp_servers`.
- Is not persisted by `claude setup-token`; the operator must capture it from stdout.

The broker must surface days-to-expiry in `broker_status` and log a warning under 30 days.

---

## 2. Architecture decision records

### ADR-001 — Wrap the Python Agent SDK, not the raw CLI

**Decision.** Build on `claude-agent-sdk` (Python) rather than spawning `claude -p` and parsing stdout.

**Rationale.** The archived reference project shells out and parses text. The SDK provides, as
first-class typed API, everything that approach would require reimplementing:

| Need | SDK feature |
|---|---|
| Persistent multi-turn process | `ClaudeSDKClient` + `client.query()` / `receive_response()` |
| In-process permission decisions | `can_use_tool` → `PermissionResultAllow` / `PermissionResultDeny` |
| Session continuity | `session_id`, `resume`, `fork_session`, `continue_conversation` |
| Spend ceiling | `max_budget_usd`, `max_turns`, `task_budget` |
| Cancellation | `client.interrupt()` |
| Durable session state | `session_store`, `session_store_flush` |
| Host-config isolation | `setting_sources`, `strict_mcp_config` |
| Rollback | `enable_file_checkpointing` |
| Deterministic interception | `hooks` + `HookMatcher` |
| Sandboxing | `sandbox: SandboxSettings` |
| Arbitrary CLI escape hatch | `extra_args` |

Parsing `stream-json` by hand would reimplement the SDK badly. `extra_args` covers any future flag the
SDK has not yet surfaced, so wrapping the SDK costs no expressiveness.

**Consequence.** The broker is Python. Pin the SDK; its surface is still moving.

---

### ADR-002 — Pin both the SDK and the CLI

**Decision.** Pin `claude-agent-sdk==0.2.129` and install `@anthropic-ai/claude-code` at a pinned
version in the image. Set `ClaudeAgentOptions.cli_path` to that install explicitly.

**Rationale.** The SDK bundles a CLI, but the bundled version is an implementation detail that moves
with SDK releases. Several behaviours this design depends on are version-gated:

| Behaviour | Minimum CLI version |
|---|---|
| `capabilities` array in `system/init` | 2.1.205 |
| `--forward-subagent-text` | 2.1.211 |
| Nested subagent messages in stream | 2.1.219 |
| `mcp_server_errors` in `system/init` | 2.1.219 |
| Stdin readable on Windows | 2.1.211 |

**Minimum supported CLI: 2.1.219.** The broker asserts this at startup and refuses to run below it.

> The developer's host currently has CLI **2.1.193**, which is below this floor. This does not matter —
> the container installs its own pinned CLI — but it does mean host-side smoke tests may behave
> differently from the container. Test in the container.

---

### ADR-003 — Non-blocking by default, with an optional bounded wait

**Decision.** `session_send` returns a `turn_id` immediately. Progress is retrieved via `session_poll`
using a monotonic integer cursor. `session_send` and `session_poll` both accept an optional `wait_ms`
(0–60000, default 0) that long-polls for up to that duration before returning whatever is available.

**Rationale.** Pure blocking breaks on client timeouts for long runs. Pure polling adds latency and
chattiness to short runs. The bounded wait collapses the common short case into one round trip while
keeping long runs safe. The cursor makes polling idempotent and resumable — a dropped connection
loses nothing.

**Rejected:** server-initiated MCP notifications for progress. Client support is inconsistent, and a
pull model degrades gracefully where a push model just fails.

---

### ADR-004 — Park-and-resolve permissions instead of blanket bypass

**Decision.** `can_use_tool` consults a policy. `allow` and `deny` resolve inline. `ask` parks the
callback on an `asyncio.Event`, surfaces the request through `permission_pending`, and waits for
`permission_resolve` or a timeout (default deny).

**Rationale.** The archived project's default was `--dangerously-skip-permissions`, which is the whole
security model in one flag. Parking lets the *calling* LLM — or a static policy, or a human — act as
the approver, which is the entire point of putting an agent in front of an agent. Timeout-to-deny
means a forgotten request fails closed.

**Consequence.** Callers must poll for pending permissions, or set a policy with no `ask` rules.
`permission_policy: "autonomous"` exists for genuinely unattended runs and is documented as
load-bearing risk. See [04](04-permission-broker.md).

---

### ADR-005 — The container is the security boundary

**Decision.** Do not rely on the permission broker for containment. Harden the container: non-root
user, read-only root filesystem, dropped capabilities, explicit volume mounts, restricted egress.

**Rationale.** The archived project stated this plainly and was right: *"This wrapper is not an
OS-level sandbox; for a hard file-system boundary, run the MCP server inside your own container."* We
adopt that as a requirement rather than a caveat. The permission broker is policy; the container is
enforcement. A prompt injection that talks its way past policy still hits the container wall.

---

### ADR-006 — Streamable HTTP transport, stdio for local dev

**Decision.** Default transport is MCP **streamable HTTP** bound to `0.0.0.0:8787` inside the
container. stdio is supported for local non-container development.

**Rationale.** stdio across a container boundary requires `docker exec -i` and couples client lifetime
to an exec session, which defeats the purpose of long-lived sessions surviving client restarts. HTTP
gives a stable endpoint the broker outlives clients behind.

**Consequence.** The endpoint requires auth. A static bearer token (`BROKER_AUTH_TOKEN`) is mandatory
when binding to anything other than loopback. See [06 §5](06-docker-auth.md).

---

### ADR-007 — Filesystem session store in v1; pluggable interface

**Decision.** Implement `SessionStore` against a mounted volume. Define the interface so Redis,
Postgres, and S3 backends can be added without touching the broker.

**Rationale.** The SDK ships reference implementations for exactly these three backends
(`examples/session_stores/{redis,postgres,s3}_session_store.py`), so the interface is proven and the
migration path is short. A filesystem store on a named volume survives container restarts, which is the
actual v1 requirement. Distributed storage is speculative until there is more than one broker instance.

**Consequence.** v1 is single-instance. Horizontal scaling is explicitly out of scope; see
[08 §5](08-implementation-plan.md).

---

## 3. Out of scope for v1

Recorded so they are not silently assumed:

- Multiple broker replicas / shared session state across instances.
- A scheduler or task queue for unattended batch work. (Offered during scoping and not selected. The
  broker's API is designed so a supervisor can be layered on top later without changes.)
- Web UI. `broker_status` and the transcript files are the operator interface.
- Automatic token renewal. `setup-token` requires a browser; renewal is a manual annual task.
- Windows containers. The image is Linux; the Windows host runs it under Docker Desktop.
