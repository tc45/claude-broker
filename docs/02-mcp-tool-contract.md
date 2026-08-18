# 02 — MCP Tool Contract

Twelve tools. This is the entire public API surface. Schemas are normative — implement exactly.

| Tool | Blocking? | Purpose |
|---|---|---|
| `session_create` | No | Start a session, return a handle |
| `session_send` | Optional | Submit a prompt as a turn |
| `session_poll` | Optional | Read events from a cursor |
| `session_interrupt` | No | Cancel the in-flight turn |
| `session_fork` | No | Branch a session at its current state |
| `session_close` | No | Terminate and release resources |
| `session_list` | No | Enumerate sessions |
| `session_transcript` | No | Full or filtered history |
| `permission_pending` | No | List parked permission requests |
| `permission_resolve` | No | Allow / deny a parked request |
| `broker_status` | No | Health, auth, budget, rate-limit state |
| `workspace_list` | No | Enumerate mounted workspace roots |

Conventions used throughout:

- All timestamps are RFC 3339 UTC with `Z`.
- All costs are USD floats, rounded to 6 decimal places.
- `session_id`, `turn_id`, `request_id` are opaque strings; do not parse them.
- Unknown fields in arguments are **rejected**, not ignored (`strict` schemas), so typos surface early.

---

## 1. Session lifecycle tools

### 1.1 `session_create`

Creates a session and its backing `claude` subprocess. Returns as soon as the subprocess reports
`system/init`, or fails.

**Arguments**

| Field | Type | Req | Default | Notes |
|---|---|:--:|---|---|
| `workspace` | string | ✔ | — | Absolute path. Must resolve under a `BROKER_WORKSPACE_ROOTS` entry after symlink resolution. |
| `model` | string | | inherit | `opus` \| `sonnet` \| `haiku` \| `fable` \| full model ID |
| `effort` | string | | inherit | `low` \| `medium` \| `high` \| `xhigh` \| `max` |
| `system_prompt_append` | string | | — | Added to the default prompt. Max 8192 chars. |
| `permission_policy` | string | | `BROKER_DEFAULT_POLICY` | `readonly` \| `reviewed` \| `autonomous` \| named policy |
| `allowed_tools` | string[] | | `[]` | Permission-rule syntax, e.g. `Bash(git diff *)` |
| `disallowed_tools` | string[] | | `[]` | |
| `max_budget_usd` | number | | `BROKER_DEFAULT_BUDGET_USD` | 0.01–100.0 |
| `max_turns` | integer | | — | 1–200 |
| `mcp_servers` | object | | `{}` | Map of name → MCP server config, passed through |
| `agents` | object | | — | Custom subagent definitions, passed through |
| `additional_directories` | string[] | | `[]` | Each validated like `workspace` |
| `setting_sources` | string[] | | `[]` | `user` \| `project` \| `local`. Empty = isolated. |
| `resume_from` | string | | — | Session ID to resume. Mutually exclusive with `fork_from`. |
| `fork_from` | string | | — | Session ID to fork. Sets `fork_session=True`. |
| `label` | string | | — | Human-readable tag for `session_list`. Max 120 chars. |
| `idle_ttl_seconds` | integer | | `BROKER_SESSION_IDLE_TTL` | 60–86400 |

**Returns**

```jsonc
{
  "session_id": "0f9c1e2a-7b44-4d1e-9a03-2c6f8d5b1e77",
  "state": "idle",
  "workspace": "/workspace/my-repo",
  "model": "claude-opus-5",
  "permission_policy": "reviewed",
  "max_budget_usd": 5.0,
  "cursor": 1,
  "capabilities": ["interrupt_receipt_v1", "interrupt_cancel_queued_v1"],
  "mcp_servers": [{"name": "github", "status": "connected"}],
  "created_at": "2026-08-04T14:22:03Z"
}
```

**Mapping to `ClaudeAgentOptions`**

```python
ClaudeAgentOptions(
    cwd=workspace,
    model=model,
    effort=effort,
    system_prompt={"type": "preset", "preset": "claude_code",
                   "append": system_prompt_append} if system_prompt_append else None,
    allowed_tools=allowed_tools,
    disallowed_tools=disallowed_tools,
    can_use_tool=permission_broker.make_callback(session_id, policy),
    permission_mode="default",          # never bypassPermissions; policy decides
    max_budget_usd=max_budget_usd,
    max_turns=max_turns,
    mcp_servers=mcp_servers,
    strict_mcp_config=True,
    agents=agents,
    add_dirs=additional_directories,
    setting_sources=setting_sources or [],
    session_id=session_id,              # broker-generated UUID4
    resume=resume_from or fork_from,
    fork_session=bool(fork_from),
    include_partial_messages=False,
    session_store=FilesystemSessionStore(state_dir / "sessions" / session_id / "store"),
    session_store_flush="batched",
    cli_path=config.cli_path,
    stderr=lambda line: log.debug("cli: %s", line),
    env={},                             # inherit; preflight already sanitised
)
```

`permission_mode` is pinned to `"default"` so that `can_use_tool` is always consulted.
`bypassPermissions` is never set by the broker — the `autonomous` policy achieves the same effect
through the policy engine, where it is auditable.

**Errors:** `WORKSPACE_INVALID`, `SESSION_LIMIT_REACHED`, `SESSION_NOT_FOUND` (bad `resume_from` /
`fork_from`), `INVALID_ARGUMENT`, `AUTH_FAILED`, `MCP_SERVER_FAILED`, `RATE_LIMITED`.

---

### 1.2 `session_send`

Submits a prompt as a new turn.

**Arguments**

| Field | Type | Req | Default | Notes |
|---|---|:--:|---|---|
| `session_id` | string | ✔ | — | |
| `prompt` | string | ✔ | — | 1–1,000,000 chars. May contain `/skill-name` invocations. |
| `wait_ms` | integer | | `0` | 0–60000. Long-poll before returning. |

**Returns**

```jsonc
{
  "turn_id": "t_01H...",
  "session_id": "0f9c...",
  "state": "running",          // or "idle" if it finished within wait_ms
  "cursor": 12,                // caller's next poll cursor
  "events": [ /* Event[] since submission, possibly empty */ ],
  "result": null,              // populated iff state == "idle"
  "pending_permissions": []
}
```

**Semantics**

- Rejects with `SESSION_BUSY` if `state == "running"`. Callers wanting parallelism create more sessions.
- Rejects with `RATE_LIMITED` if the governor is in cooldown; the error carries `retry_after_seconds`.
- Rejects with `BUDGET_EXCEEDED` if the session or global ledger is exhausted.
- With `wait_ms > 0`, returns early the moment the turn reaches a terminal state **or** a permission
  request is parked. Do not burn the full wait when a decision is needed.

**Errors:** `SESSION_NOT_FOUND`, `SESSION_BUSY`, `SESSION_TERMINAL`, `RATE_LIMITED`,
`BUDGET_EXCEEDED`, `INVALID_ARGUMENT`.

---

### 1.3 `session_poll`

Reads events from a cursor. Idempotent and safe to retry.

**Arguments**

| Field | Type | Req | Default | Notes |
|---|---|:--:|---|---|
| `session_id` | string | ✔ | — | |
| `cursor` | integer | | `0` | Return events with index ≥ cursor |
| `wait_ms` | integer | | `0` | 0–60000. Long-poll if no events past cursor. |
| `limit` | integer | | `200` | 1–1000 |
| `include` | string[] | | all | Filter by event type |

**Returns**

```jsonc
{
  "session_id": "0f9c...",
  "state": "awaiting_permission",
  "cursor": 31,                 // next cursor; pass back verbatim
  "has_more": false,            // true if truncated by limit
  "events": [
    {"index": 12, "type": "assistant_text", "at": "2026-08-04T14:23:01Z",
     "text": "I'll start by reading the auth module."},
    {"index": 13, "type": "tool_use", "at": "...", "tool": "Read",
     "tool_use_id": "toolu_01...", "input": {"file_path": "/workspace/src/auth.py"}},
    {"index": 14, "type": "tool_result", "at": "...", "tool_use_id": "toolu_01...",
     "is_error": false, "summary": "412 lines"},
    {"index": 30, "type": "permission_request", "at": "...", "request_id": "p_03",
     "tool": "Bash", "input": {"command": "pytest -q"}, "expires_at": "..."}
  ],
  "pending_permissions": [{"request_id": "p_03", "tool": "Bash", "expires_at": "..."}],
  "cost": {"turn_usd": 0.184, "session_usd": 0.912}
}
```

**Event types**

| `type` | Emitted for | Key fields |
|---|---|---|
| `session_init` | `SystemMessage(subtype="init")` | `model`, `tools`, `mcp_servers`, `capabilities` |
| `assistant_text` | `TextBlock` | `text` |
| `thinking` | thinking block | `text` (omitted unless `include` requests it) |
| `tool_use` | `ToolUseBlock` | `tool`, `tool_use_id`, `input`, `parent_tool_use_id` |
| `tool_result` | `ToolResultBlock` | `tool_use_id`, `is_error`, `summary`, `content` |
| `permission_request` | Parked `can_use_tool` | `request_id`, `tool`, `input`, `expires_at` |
| `permission_decision` | Resolution | `request_id`, `decision`, `decided_by`, `reason` |
| `api_retry` | `SystemMessage(subtype="api_retry")` | `attempt`, `max_retries`, `retry_delay_ms`, `error` |
| `turn_result` | `ResultMessage` | `turn_id`, `is_error`, `num_turns`, `total_cost_usd`, `usage`, `result`, `stop_reason` |
| `state_change` | Broker transition | `from`, `to`, `reason` |
| `error` | Broker/SDK failure | `code`, `message` |

`tool_result.content` is truncated to 4096 chars with `truncated: true`; `summary` is always a short
human-readable line. Full content is available via `session_transcript`.

**Errors:** `SESSION_NOT_FOUND`, `INVALID_ARGUMENT`.

---

### 1.4 `session_interrupt`

Cancels the in-flight turn. Relies on the always-running consumer task ([01 §3](01-architecture.md)).

**Arguments:** `session_id` (string, required).

**Returns**

```jsonc
{"session_id": "0f9c...", "state": "idle", "interrupted_turn_id": "t_01H...", "cursor": 44}
```

No-op returning `state` unchanged if nothing is running. Waits up to 10 s for the SDK to acknowledge;
if it does not, transitions to `error` and reports `INTERRUPT_TIMEOUT`.

---

### 1.5 `session_fork`

Branches a session. The parent is untouched and remains usable.

**Arguments:** `session_id` (required), plus optional overrides `label`, `model`, `permission_policy`,
`max_budget_usd`, `workspace`.

**Returns:** the same shape as `session_create`, with `parent_session_id` populated.

Implemented as `session_create(resume=<parent>, fork_session=True)`. Rejects with `SESSION_BUSY` if the
parent has a turn in flight — fork from a settled state only, so the branch point is unambiguous.

---

### 1.6 `session_close`

**Arguments:** `session_id` (required), `reason` (string, optional).

Cancels the consumer task, calls `client.disconnect()`, flushes the event log and `meta.json`,
transitions to `closed`. Idempotent.

**Returns**

```jsonc
{"session_id": "0f9c...", "state": "closed",
 "final_cost_usd": 1.402, "turns": 7, "transcript_path": "/var/lib/claude-broker/sessions/0f9c.../events.jsonl"}
```

---

### 1.7 `session_list`

**Arguments:** `state` (string[], optional filter), `include_closed` (bool, default `false`),
`limit` (default 50).

**Returns**

```jsonc
{"sessions": [
  {"session_id": "0f9c...", "label": "auth refactor", "state": "running",
   "workspace": "/workspace/my-repo", "model": "claude-opus-5",
   "cost_usd": 0.912, "budget_usd": 5.0, "turns": 3, "cursor": 31,
   "pending_permissions": 1, "created_at": "...", "last_active_at": "...",
   "parent_session_id": null}
], "total": 1, "capacity": {"live": 1, "max": 8}}
```

---

### 1.8 `session_transcript`

Full history, including content truncated in `session_poll`.

**Arguments:** `session_id` (required), `format` (`json` | `markdown`, default `json`), `from_cursor`
(default 0), `to_cursor` (optional), `include` (event-type filter).

**Returns:** `{"session_id", "format", "from_cursor", "to_cursor", "content"}` where `content` is an
`Event[]` for `json` or a rendered string for `markdown`.

Works on closed sessions by reading `events.jsonl` from disk.

---

## 2. Permission tools

### 2.1 `permission_pending`

**Arguments:** `session_id` (optional — omit for all sessions).

**Returns**

```jsonc
{"pending": [
  {"request_id": "p_03", "session_id": "0f9c...", "tool": "Bash",
   "input": {"command": "pytest -q", "description": "Run the test suite"},
   "matched_rule": "ask: Bash(pytest *)",
   "suggestions": [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "pytest *"}]}],
   "requested_at": "2026-08-04T14:24:10Z", "expires_at": "2026-08-04T14:29:10Z"}
]}
```

`suggestions` is passed through from `ToolPermissionContext.suggestions` — these are Claude's own
proposed permission updates, which the caller may accept wholesale via `permission_resolve`.

### 2.2 `permission_resolve`

**Arguments**

| Field | Type | Req | Default | Notes |
|---|---|:--:|---|---|
| `request_id` | string | ✔ | — | |
| `decision` | string | ✔ | — | `allow` \| `deny` |
| `reason` | string | | — | Recorded; sent to Claude on deny |
| `updated_input` | object | | — | `allow` only. Replaces tool input. |
| `interrupt` | boolean | | `false` | `deny` only. Aborts the whole turn. |
| `remember` | string | | `none` | `none` \| `session`. `session` adds an allow/deny rule for the rest of the session. |

Maps to `PermissionResultAllow(updated_input=...)` or
`PermissionResultDeny(message=reason, interrupt=interrupt)`.

`remember: "session"` appends a rule to the session's in-memory policy overlay. It is deliberately not
persisted to the policy file — durable policy changes are an operator action, not an agent action.

**Returns:** `{"request_id", "session_id", "decision", "applied": true, "state": "running"}`

**Errors:** `PERMISSION_NOT_FOUND` (unknown or already resolved), `PERMISSION_EXPIRED`,
`INVALID_ARGUMENT`.

---

## 3. Operations tools

### 3.1 `broker_status`

```jsonc
{
  "version": "1.0.0",
  "uptime_seconds": 84213,
  "auth": {
    "method": "CLAUDE_CODE_OAUTH_TOKEN",
    "billing": "subscription",
    "token_expires_at": "2027-05-30T00:00:00Z",
    "days_remaining": 299,
    "warning": null
  },
  "cli": {"version": "2.1.219", "path": "/usr/local/bin/claude"},
  "sdk": {"version": "0.2.129"},
  "sessions": {"live": 2, "idle": 1, "running": 1, "max": 8},
  "budget": {"global_spent_usd": 14.83, "global_limit_usd": 100.0, "window_started_at": "..."},
  "rate_limit": {"state": "ok", "cooldown_until": null, "recent_retries": 0},
  "workspace_roots": ["/workspace"],
  "health": "ok"
}
```

`auth.billing` is computed from which credential actually won precedence, **not** from which one was
supplied. It reads `subscription` only when `CLAUDE_CODE_OAUTH_TOKEN` or interactive OAuth is the
active credential. Anything else reports `api` and sets `health: "degraded"` with a warning — this is
the runtime half of the [00 §1.3](00-decisions.md) guard.

`token_expires_at` is decoded from the token's own claims where available; otherwise it is null and
`days_remaining` is omitted rather than guessed.

### 3.2 `workspace_list`

**Returns:** `{"roots": [{"path": "/workspace", "writable": true, "entries": ["my-repo", "scratch"]}]}`

Lets a calling LLM discover valid `workspace` values without guessing paths and eating
`WORKSPACE_INVALID` errors.

---

## 4. Error taxonomy

Every error returns an MCP tool error whose body is:

```jsonc
{"error": {"code": "SESSION_BUSY", "message": "…", "retryable": true, "details": {}}}
```

| Code | Retryable | Meaning |
|---|:--:|---|
| `INVALID_ARGUMENT` | ✖ | Schema or range violation |
| `SESSION_NOT_FOUND` | ✖ | Unknown or reaped session |
| `SESSION_BUSY` | ✔ | Turn already running |
| `SESSION_TERMINAL` | ✖ | Session is `closed` or `error` |
| `SESSION_LIMIT_REACHED` | ✔ | `BROKER_MAX_SESSIONS` hit |
| `WORKSPACE_INVALID` | ✖ | Outside allowed roots, missing, or not a directory |
| `PERMISSION_NOT_FOUND` | ✖ | Unknown or already-resolved request |
| `PERMISSION_EXPIRED` | ✖ | Deadline passed; auto-denied |
| `BUDGET_EXCEEDED` | ✖ | Session or global ceiling reached |
| `RATE_LIMITED` | ✔ | Cooldown active; `details.retry_after_seconds` |
| `AUTH_FAILED` | ✖ | Token invalid, expired, or plan lapsed |
| `MCP_SERVER_FAILED` | ✖ | A declared child MCP server did not load |
| `INTERRUPT_TIMEOUT` | ✖ | SDK did not acknowledge within 10 s |
| `CLI_UNAVAILABLE` | ✖ | Binary missing or below the 2.1.219 floor |
| `INTERNAL` | ✔ | Unexpected; details are logged, not returned |

`retryable: true` means retrying the identical call may succeed later. Clients should honour
`details.retry_after_seconds` when present rather than tight-looping.
