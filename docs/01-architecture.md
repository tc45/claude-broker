# 01 — Architecture

---

## 1. System context

```
┌────────────────────────────────────────────────────────────────────┐
│  Your LLM client  (Claude Desktop, Cursor, custom agent, script)   │
└───────────────────────────────┬────────────────────────────────────┘
                                │  MCP over streamable HTTP
                                │  Authorization: Bearer <BROKER_AUTH_TOKEN>
┌───────────────────────────────▼────────────────────────────────────┐
│  Docker container: claude-broker                                   │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ MCP facade  (FastMCP)                                        │  │
│  │  12 tools — see 02-mcp-tool-contract.md                      │  │
│  └───────────────────────────┬──────────────────────────────────┘  │
│                              │ in-process async calls              │
│  ┌───────────────────────────▼──────────────────────────────────┐  │
│  │ Broker core                                                  │  │
│  │  SessionRegistry · EventLog · PermissionBroker · CostLedger  │  │
│  │  RateLimitGovernor · Reaper                                  │  │
│  └───────────────────────────┬──────────────────────────────────┘  │
│                              │ one ClaudeSDKClient per session     │
│  ┌───────────────────────────▼──────────────────────────────────┐  │
│  │ claude-agent-sdk 0.2.129                                     │  │
│  └───────────────────────────┬──────────────────────────────────┘  │
│                              │ subprocess, stream-json duplex      │
│  ┌───────────────────────────▼──────────────────────────────────┐  │
│  │ claude CLI ≥2.1.219   (Node 22)                              │  │
│  │  auth: CLAUDE_CODE_OAUTH_TOKEN → your Max 20x subscription   │  │
│  └───────────────────────────┬──────────────────────────────────┘  │
│                              │                                     │
│   volumes:  /workspace  (rw, your code)                            │
│             /var/lib/claude-broker  (rw, sessions + transcripts)   │
└──────────────────────────────┼─────────────────────────────────────┘
                               │ HTTPS
                               ▼
                    api.anthropic.com
```

**Key property:** the broker process outlives any individual MCP client connection. Sessions persist
across client restarts; a dropped connection is recovered by re-polling from a cursor.

---

## 2. Components

### 2.1 MCP facade — `broker/mcp_server.py`

Thin. Translates MCP tool calls into `BrokerCore` method calls and serialises results. Contains **no
business logic** — every rule lives in the core so it is testable without an MCP client.

Responsibilities: schema validation, bearer-token auth, error mapping to the taxonomy in
[02 §4](02-mcp-tool-contract.md), request-scoped logging.

### 2.2 SessionRegistry — `broker/registry.py`

Owns the `dict[SessionId, Session]`. A `Session` holds:

| Field | Type | Notes |
|---|---|---|
| `session_id` | `str` (UUID4) | Also passed to the SDK as `session_id` |
| `state` | `SessionState` | See [03 §1](03-session-broker.md) |
| `client` | `ClaudeSDKClient` | Live subprocess handle |
| `options` | `ClaudeAgentOptions` | Frozen at creation |
| `event_log` | `EventLog` | Append-only, cursor-addressed |
| `current_turn` | `Turn \| None` | In-flight turn, if any |
| `cost` | `SessionCost` | Cumulative |
| `created_at` / `last_active_at` | `datetime` (UTC) | Drives the reaper |
| `workspace` | `Path` | Must resolve under an allowed root |
| `parent_session_id` | `str \| None` | Set on fork |
| `consumer_task` | `asyncio.Task` | Drains `receive_messages()` |

Concurrency: one `asyncio.Lock` per session. All state transitions take it. A turn is rejected with
`SESSION_BUSY` if one is already running — the broker never queues implicitly, because silent queuing
makes cost attribution and interrupts ambiguous.

### 2.3 EventLog — `broker/event_log.py`

Append-only list of normalised events per session, addressed by a monotonic integer **cursor** starting
at 0. This is the backbone of ADR-003.

Every SDK message becomes one event. Events are also streamed to
`/var/lib/claude-broker/sessions/<id>/events.jsonl` as they are appended, so a crash loses at most the
unflushed tail.

In-memory retention is capped at `BROKER_EVENT_MEMORY_LIMIT` (default 5000) events per session; older
events are served from the JSONL file on demand. `session_poll` for a cursor below the in-memory
window reads through to disk transparently.

### 2.4 PermissionBroker — `broker/permissions.py`

Implements `can_use_tool`. Full design in [04](04-permission-broker.md).

### 2.5 CostLedger + RateLimitGovernor — `broker/budget.py`

Accumulates `total_cost_usd` per session and globally; watches for `api_retry` system events and gates
new turns during cooldown. Full design in [05](05-budget-ratelimit.md).

### 2.6 Reaper — `broker/reaper.py`

Background task, runs every 60 s:

- Sessions `idle` longer than `BROKER_SESSION_IDLE_TTL` (default 3600 s) → `close()`.
- Sessions in a terminal state older than `BROKER_SESSION_RETAIN` (default 86400 s) → drop from
  registry; transcripts on disk are kept.
- Pending permission requests past their deadline → resolved as `deny`.

---

## 3. The consumer-task pattern

This is the single most important implementation detail; get it wrong and interrupts hang.

`ClaudeSDKClient.interrupt()` only takes effect while messages are actively being consumed. The broker
therefore **never** iterates `receive_response()` inline in a request handler. Instead, on session
creation it starts one long-lived consumer task:

```python
async def _consume(session: Session) -> None:
    """Owns the message stream for a session's entire lifetime."""
    try:
        async for message in session.client.receive_messages():
            event = normalise(message)                 # -> Event
            session.event_log.append(event)            # bumps cursor
            await _apply_side_effects(session, message)
            session.wakeup.set()                       # release long-pollers
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        session.transition(SessionState.ERROR, reason=repr(exc))
        session.wakeup.set()
```

`_apply_side_effects` handles the transitions that depend on message content:

| Message | Effect |
|---|---|
| `SystemMessage(subtype="init")` | Record model, tools, `mcp_servers`, `mcp_server_errors`, `capabilities`. Fail the session if `mcp_server_errors` is non-empty and `strict_mcp_config` is set. |
| `SystemMessage(subtype="api_retry")` | Feed `RateLimitGovernor`; if `error == "rate_limit"`, enter cooldown. |
| `AssistantMessage` | Append; no transition. |
| `ResultMessage` | Close the current turn, add `total_cost_usd` to the ledger, transition `running → idle`. |

`session_send` writes to the client and returns; `session_poll` reads the event log. Neither touches
the stream. This keeps `interrupt()` responsive at all times.

---

## 4. Repository layout

```
claude_wrapper/
├── README.md
├── docs/                          # these specifications
├── pyproject.toml                 # uv/hatch; pins claude-agent-sdk==0.2.129
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example                   # never contains real tokens
├── policies/
│   └── default.yaml               # shipped permission policies
└── src/broker/
    ├── __init__.py
    ├── __main__.py                # entrypoint; arg parsing; transport selection
    ├── config.py                  # env → Config dataclass; startup preflight
    ├── mcp_server.py              # FastMCP facade, 12 tools
    ├── core.py                    # BrokerCore: orchestrates the rest
    ├── registry.py                # SessionRegistry, Session, SessionState
    ├── event_log.py               # EventLog, Event, cursor, JSONL sink
    ├── normalise.py               # SDK message -> Event
    ├── permissions.py             # PermissionBroker, PolicyEngine, PendingRequest
    ├── budget.py                  # CostLedger, RateLimitGovernor
    ├── session_store.py           # FilesystemSessionStore (SDK SessionStore impl)
    ├── reaper.py
    ├── errors.py                  # BrokerError hierarchy -> MCP error codes
    └── workspace.py               # path validation / traversal defence
└── tests/
    ├── conftest.py                # FakeSDKClient fixture
    ├── unit/
    ├── integration/               # real CLI, cheap prompts, marked `live`
    └── fixtures/
```

---

## 5. On-disk layout (`/var/lib/claude-broker`)

```
/var/lib/claude-broker/
├── sessions/
│   └── <session_id>/
│       ├── meta.json         # creation options, parent, timestamps, final state
│       ├── events.jsonl      # append-only normalised event stream
│       └── store/            # SDK SessionStore payload (conversation state)
└── ledger.jsonl              # one record per completed turn: cost, tokens, model
```

`meta.json` is written at creation and rewritten on every state transition. `events.jsonl` and
`ledger.jsonl` are append-only and fsynced per `session_store_flush` policy (`"batched"` by default).

---

## 6. Configuration

All configuration is environment variables, resolved once at startup into a frozen `Config` dataclass.

| Variable | Default | Meaning |
|---|---|---|
| `BROKER_TRANSPORT` | `http` | `http` or `stdio` |
| `BROKER_HOST` | `0.0.0.0` | Bind address |
| `BROKER_PORT` | `8787` | Bind port |
| `BROKER_AUTH_TOKEN` | *(none)* | Required unless bound to loopback |
| `CLAUDE_CODE_OAUTH_TOKEN` | *(none)* | **Required.** From `claude setup-token` |
| `BROKER_WORKSPACE_ROOTS` | `/workspace` | Colon-separated allowed roots |
| `BROKER_STATE_DIR` | `/var/lib/claude-broker` | Persistence root |
| `BROKER_DEFAULT_POLICY` | `reviewed` | `readonly` \| `reviewed` \| `autonomous` |
| `BROKER_POLICY_FILE` | `policies/default.yaml` | Policy definitions |
| `BROKER_MAX_SESSIONS` | `8` | Hard cap on concurrent live sessions |
| `BROKER_SESSION_IDLE_TTL` | `3600` | Seconds before an idle session is reaped |
| `BROKER_SESSION_RETAIN` | `86400` | Seconds a terminal session stays listable |
| `BROKER_EVENT_MEMORY_LIMIT` | `5000` | In-memory events per session |
| `BROKER_PERMISSION_TIMEOUT` | `300` | Seconds before a parked request is denied |
| `BROKER_GLOBAL_BUDGET_USD` | *(none)* | Optional ceiling across all sessions |
| `BROKER_DEFAULT_BUDGET_USD` | `2.00` | Per-session default `max_budget_usd` |
| `BROKER_ALLOW_API_BILLING` | `0` | Escape hatch for the §1.3 preflight guard |
| `BROKER_CLI_PATH` | `/usr/local/bin/claude` | Passed as `cli_path` |
| `BROKER_LOG_LEVEL` | `INFO` | |

`BROKER_MAX_SESSIONS` defaults to 8 because each live session is a Node subprocess holding a model
context; memory, not CPU, is the binding constraint.
