# 07 — Testing

Three tiers. Tiers 1 and 2 must pass in CI with no credentials and no network. Tier 3 spends real plan
usage and runs on demand.

| Tier | Marker | Credentials | Runtime | When |
|---|---|---|---|---|
| Unit | *(none)* | none | < 10 s | Every commit |
| Integration (faked SDK) | `integration` | none | < 60 s | Every commit |
| Live | `live` | real token | minutes, costs money | Pre-release, manual |

`pytest -m "not live"` is the CI default.

---

## 1. The fake SDK client

Tier 2 depends entirely on this fixture. Build it first — it determines how testable everything else is.

`FakeSDKClient` implements the `ClaudeSDKClient` surface the broker uses (`query`, `receive_messages`,
`interrupt`, `disconnect`, async context manager) and is driven by a **scripted message sequence**:

```python
client = FakeSDKClient(script=[
    SystemMessage(subtype="init", data={"model": "claude-opus-5", "tools": ["Read", "Bash"],
                                        "mcp_servers": [], "capabilities": ["interrupt_receipt_v1"]}),
    AssistantMessage(content=[TextBlock(text="Reading the file.")]),
    ToolUseRequest(tool="Read", input={"file_path": "/workspace/a.py"}),   # triggers can_use_tool
    ResultMessage(subtype="success", is_error=False, num_turns=2, session_id="...",
                  total_cost_usd=0.0412, duration_ms=3100, duration_api_ms=2900,
                  result="Done.", stop_reason="end_turn"),
])
```

It must support: injected delays between messages, raising mid-stream, blocking until released (for
interrupt tests), and invoking the broker's `can_use_tool` so the permission path is exercised without
a real model.

---

## 2. Unit tests

### 2.1 Config & preflight — `test_config.py`

| # | Test | Expect |
|---|---|---|
| U1 | `ANTHROPIC_API_KEY` set, `BROKER_ALLOW_API_BILLING` unset | `ConfigError` naming the variable |
| U2 | `ANTHROPIC_AUTH_TOKEN` set | `ConfigError` |
| U3 | Each of `CLAUDE_CODE_USE_{BEDROCK,VERTEX,FOUNDRY}` | `ConfigError` |
| U4 | `ANTHROPIC_API_KEY` + `BROKER_ALLOW_API_BILLING=1` | Starts; `billing_mode == "api"`; warning logged |
| U5 | No `CLAUDE_CODE_OAUTH_TOKEN` | `ConfigError` with the `setup-token` remediation |
| U6 | CLI reports 2.1.218 | `ConfigError` citing the 2.1.219 floor |
| U7 | CLI reports 2.1.219 / 2.2.0 | Passes |
| U8 | `bare` in passthrough extra args | `ConfigError` |
| U9 | HTTP transport, `0.0.0.0`, no auth token | `ConfigError` |
| U10 | HTTP transport, `127.0.0.1`, no auth token | Passes |

### 2.2 Workspace validation — `test_workspace.py`

| # | Input | Expect |
|---|---|---|
| U11 | `/workspace/repo` under root | Allowed |
| U12 | `/etc/passwd` | `WORKSPACE_INVALID` |
| U13 | `/workspace/../etc` | `WORKSPACE_INVALID` |
| U14 | Symlink `/workspace/link` → `/etc` | `WORKSPACE_INVALID` after resolution |
| U15 | Path that does not exist | `WORKSPACE_INVALID` |
| U16 | File, not a directory | `WORKSPACE_INVALID` |
| U17 | Relative path `repo` | `INVALID_ARGUMENT` (absolute required) |

### 2.3 Policy engine — `test_policy.py`

| # | Test | Expect |
|---|---|---|
| U18 | First match wins on ordered rules | Earlier rule applies |
| U19 | `Bash(git diff *)` vs `git diff-index` | Does **not** match (space significance) |
| U20 | `Bash(git diff *)` vs `git diff HEAD` | Matches |
| U21 | Policy where `Bash` precedes `Bash(rm *)` | Loader raises shadowing error |
| U22 | `mcp__*` vs `mcp__github__create_issue` | Matches |
| U23 | No rule matches | Policy `default` applies |
| U24 | `workspace_write` guard, path inside workspace | Allow stands |
| U25 | `workspace_write` guard, path outside | Downgraded to deny |
| U26 | `workspace_write` guard, symlink escaping workspace | Downgraded to deny |
| U27 | `no_push` guard on `git push origin main` | Downgraded to ask |
| U28 | `readonly` policy vs `Write` | Deny |
| U29 | `autonomous` policy vs `Bash(sudo rm)` | Deny (deny-list beats `default: allow`) |

### 2.4 Event log — `test_event_log.py`

| # | Test | Expect |
|---|---|---|
| U30 | Cursor increments monotonically | Strictly increasing, never reused |
| U31 | Poll from cursor N | Only events with index ≥ N |
| U32 | Poll beyond the end | Empty list, cursor unchanged |
| U33 | Exceed `BROKER_EVENT_MEMORY_LIMIT` | Old events served from JSONL, contents identical |
| U34 | `limit` truncation | `has_more: true`, cursor at the truncation point |
| U35 | Every event appears in `events.jsonl` in order | Byte-for-byte replay matches |
| U36 | Unmapped SDK message type | `error` event with `UNMAPPED_MESSAGE`, not a drop |

### 2.5 Cost ledger — `test_budget.py`

| # | Test | Expect |
|---|---|---|
| U37 | Accumulate across turns | Total equals the sum |
| U38 | `ResultMessage` with `total_cost_usd: None` | Treated as 0, warning logged, no crash |
| U39 | Global budget exceeded | `session_send` → `BUDGET_EXCEEDED` |
| U40 | Ledger replay at startup | Total matches pre-restart |
| U41 | Ledger replay respects the monthly window | Prior-month records excluded |
| U42 | `by_model` attribution from `model_usage` | Per-model totals correct |

### 2.6 Rate-limit governor — `test_ratelimit.py`

| # | Test | Expect |
|---|---|---|
| U43 | `api_retry` with `error: "rate_limit"` | Cooldown entered |
| U44 | `api_retry` with `error: "server_error"` | No cooldown |
| U45 | `api_retry` with `error: "authentication_failed"` | Fail fast, `AUTH_FAILED`, no retry |
| U46 | `api_retry` with `error: "billing_error"` | Fail fast |
| U47 | Consecutive rate limits | Exponential backoff, capped at `BROKER_COOLDOWN_MAX` |
| U48 | Successful turn after a rate limit | `consecutive_rate_limits` resets to 0 |
| U49 | Cooldown is global | All sessions refuse sends, not just the throttled one |
| U50 | Jitter applied | 100 samples are not all identical |
| U51 | In-flight turn during cooldown | Not interrupted |

---

## 3. Integration tests (faked SDK)

### 3.1 Session lifecycle — `test_session_lifecycle.py`

| # | Test | Expect |
|---|---|---|
| I1 | `session_create` → `session_send` → `session_poll` to completion | State `idle`, `turn_result` present |
| I2 | `session_send` while running | `SESSION_BUSY` |
| I3 | `session_send` to a closed session | `SESSION_TERMINAL` |
| I4 | `session_send` to an unknown ID | `SESSION_NOT_FOUND` |
| I5 | Create beyond `BROKER_MAX_SESSIONS` | `SESSION_LIMIT_REACHED` |
| I6 | `session_close` twice | Idempotent, no error |
| I7 | Two sessions run concurrently | Neither blocks the other; costs attributed separately |
| I8 | `session_list` filtering by state | Correct subset |
| I9 | Fake client raises mid-stream | State `error`, `error` event, no hang |
| I10 | `mcp_server_errors` non-empty with `strict_mcp_config` | `MCP_SERVER_FAILED` |

### 3.2 Non-blocking semantics — `test_polling.py`

| # | Test | Expect |
|---|---|---|
| I11 | `wait_ms: 0` on a slow turn | Returns immediately, `state: running` |
| I12 | `wait_ms: 5000`, turn settles at 1 s | Returns at ~1 s, not 5 s |
| I13 | `wait_ms: 1000`, turn takes 10 s | Returns at ~1 s with partial events |
| I14 | `wait_ms: 30000`, permission parked at 500 ms | **Returns at ~500 ms** (no deadlock) |
| I15 | Repeated poll at the same cursor | Identical results (idempotent) |
| I16 | Poll across a restart from `events.jsonl` | Same events served |

### 3.3 Permissions — `test_permissions.py`

| # | Test | Expect |
|---|---|---|
| I17 | Policy `allow` | No parking; `PermissionResultAllow` |
| I18 | Policy `deny` | `PermissionResultDeny` with reason; turn continues |
| I19 | Policy `ask` | Parked; state `awaiting_permission`; appears in `permission_pending` |
| I20 | `permission_resolve` allow | Turn resumes; `PermissionResultAllow` |
| I21 | `permission_resolve` deny with `interrupt: true` | Turn aborts |
| I22 | `permission_resolve` allow with `updated_input` | Modified input reaches the SDK |
| I23 | Timeout expires | Auto-denied; audit `decided_by: "timeout"` |
| I24 | Resolve an unknown request | `PERMISSION_NOT_FOUND` |
| I25 | Resolve the same request twice | Second → `PERMISSION_NOT_FOUND` |
| I26 | Resolve an expired request | `PERMISSION_EXPIRED` |
| I27 | `remember: "session"` | Second identical call auto-allows |
| I28 | Overlay not inherited by a fork | Fork re-asks |
| I29 | Turn ends with a request still parked | Auto-denied, invariant 4 holds |
| I30 | `context.suggestions` forwarded | Present in `permission_pending` output |
| I31 | Every decision audited, including allows | `permissions.jsonl` complete |

### 3.4 Interrupt — `test_interrupt.py`

| # | Test | Expect |
|---|---|---|
| I32 | Interrupt a running turn | State `idle`, turn `interrupted` |
| I33 | Interrupt when idle | No-op, no error |
| I34 | Interrupt during `awaiting_permission` | Request denied with `interrupt`, turn aborts |
| I35 | SDK never acknowledges | `INTERRUPT_TIMEOUT` after 10 s, state `error` |
| I36 | Send after interrupt | Succeeds |

**I32 is the regression test for the consumer-task pattern** ([01 §3](01-architecture.md)). If someone
"simplifies" the broker to iterate `receive_response()` inline in the request handler, this test hangs.
Give it an explicit 15 s timeout so it fails loudly rather than stalling CI.

### 3.5 Resume & fork — `test_resume_fork.py`

| # | Test | Expect |
|---|---|---|
| I37 | Fork an idle session | New ID, `parent_session_id` set, parent untouched |
| I38 | Fork a running session | `SESSION_BUSY` |
| I39 | Parent and fork diverge | Independent event logs and costs |
| I40 | `resume_from` after close | New session, history available |
| I41 | `resume_from` an unknown ID | `SESSION_NOT_FOUND` |

### 3.6 Persistence — `test_persistence.py`

| # | Test | Expect |
|---|---|---|
| I42 | Restart with a session in `running` | Rewritten to `error`, reason "broker restarted" |
| I43 | Transcript readable after restart | Full history intact |
| I44 | Ledger survives restart | Global total preserved |
| I45 | `session_transcript` on a closed session | Reads from disk |
| I46 | Corrupt trailing line in `events.jsonl` | Truncated at the last valid record, warning, no crash |

### 3.7 MCP facade — `test_mcp_facade.py`

| # | Test | Expect |
|---|---|---|
| I47 | Every tool advertises a valid JSON Schema | Schema validates |
| I48 | Unknown argument field | `INVALID_ARGUMENT` (strict schemas) |
| I49 | Out-of-range `wait_ms` (−1, 60001) | `INVALID_ARGUMENT` |
| I50 | Missing bearer token, non-loopback | 401 |
| I51 | Wrong bearer token | 401, constant-time comparison |
| I52 | Every `BrokerError` maps to a taxonomy code | No `INTERNAL` leakage of stack traces |
| I53 | `broker_status` with API billing active | `health: "degraded"` + warning |

---

## 4. Live tests (`-m live`)

Real token, real spend. Keep prompts trivial; assert on mechanics, not model output.

| # | Test | Expect | Est. cost |
|---|---|---|---|
| L1 | Preflight auth probe | Succeeds; `billing == "subscription"` | < $0.01 |
| L2 | `session_create` + "reply with OK" | `turn_result` with non-null `total_cost_usd` | < $0.01 |
| L3 | Read a file under `reviewed` | Allowed without parking | < $0.02 |
| L4 | `Bash` under `reviewed` | Parks; resolve; completes | < $0.05 |
| L5 | Multi-turn context retention | Turn 2 references turn 1 | < $0.05 |
| L6 | Fork, then diverge | Both branches usable | < $0.10 |
| L7 | Interrupt a long task | Stops within 10 s | < $0.10 |
| L8 | `max_budget_usd: 0.01` on a large task | SDK halts with a budget stop reason | ~$0.01 |
| L9 | `--bare` reachable? | Confirms preflight blocks it | $0 |
| L10 | Token expiry decode | `broker_status.auth.days_remaining` plausible | $0 |

**L8 is the important one.** It is the only test that proves the spend ceiling actually binds. Run it
before every release.

---

## 5. Coverage gates

| Module | Minimum line coverage |
|---|---|
| `permissions.py` | 95% |
| `budget.py` | 95% |
| `config.py` | 95% |
| `registry.py`, `event_log.py` | 90% |
| Everything else | 80% |

The three at 95% are the ones where a bug costs money or safety rather than convenience.
