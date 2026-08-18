# 03 — Session Broker

---

## 1. State machine

```
                      session_create
                            │
                            ▼
                     ┌─────────────┐
                     │  creating   │
                     └──────┬──────┘
                 init ok    │    init failed
              ┌─────────────┴─────────────┐
              ▼                           ▼
        ┌──────────┐                ┌──────────┐
   ┌───▶│   idle   │                │  error   │◀──── unrecoverable
   │    └────┬─────┘                └────┬─────┘      SDK/CLI failure
   │         │ session_send              │
   │         ▼                           │
   │   ┌───────────┐                     │
   │   │  running  │─────────────────────┤
   │   └─┬───────┬─┘  fatal error        │
   │     │       │                       │
   │     │       │ can_use_tool -> ask   │
   │     │       ▼                       │
   │     │  ┌──────────────────────┐     │
   │     │  │ awaiting_permission  │     │
   │     │  └──────────┬───────────┘     │
   │     │             │ resolve/timeout │
   │     │◀────────────┘                 │
   │     │                               │
   │     │ ResultMessage                 │
   └─────┤                               │
         │ rate_limit detected           │
         ▼                               │
   ┌──────────────┐                      │
   │  cooldown    │──── expires ────▶ idle│
   └──────────────┘                      │
                                         │
         session_close (from any state)  │
                     │                   │
                     ▼                   ▼
                ┌──────────┐        ┌──────────┐
                │  closed  │        │  closed  │
                └──────────┘        └──────────┘
```

**States**

| State | Accepts `session_send`? | Meaning |
|---|:--:|---|
| `creating` | ✖ | Subprocess starting; awaiting `system/init` |
| `idle` | ✔ | Live, no turn in flight |
| `running` | ✖ | Turn in flight |
| `awaiting_permission` | ✖ | Turn in flight, blocked on a parked decision |
| `cooldown` | ✖ | Rate-limited; refuses turns until the deadline |
| `error` | ✖ | Terminal. Subprocess dead or unrecoverable. |
| `closed` | ✖ | Terminal. Cleanly shut down. |

`awaiting_permission` is a substate of `running` for the purposes of `interrupt` — an interrupt during
`awaiting_permission` resolves the parked request as `deny(interrupt=True)` and then aborts the turn.

**Invariants** (assert these in code; they are cheap and catch real bugs):

1. Exactly one `Turn` is non-terminal iff `state ∈ {running, awaiting_permission}`.
2. The consumer task is alive iff `state ∉ {closed, error}`.
3. `cursor` is strictly monotonic per session and never rewinds.
4. Every parked permission request belongs to the current turn. Turn end resolves all stragglers as
   `deny`.

---

## 2. Turn model

A **turn** is one `session_send` and everything it produces up to the `ResultMessage`.

```python
@dataclass
class Turn:
    turn_id: str                  # "t_" + ULID, sortable
    session_id: str
    prompt: str
    started_at: datetime
    start_cursor: int             # first event index belonging to this turn
    end_cursor: int | None
    state: Literal["running", "completed", "failed", "interrupted"]
    result_text: str | None
    cost_usd: float | None
    usage: dict | None
    num_turns: int | None         # SDK-internal agentic turns, not broker turns
    stop_reason: str | None
    permission_requests: list[str]
```

Note the naming collision: the SDK's `ResultMessage.num_turns` counts *agentic loop iterations* within
one broker turn. Do not conflate them. `session_list.turns` counts broker turns.

Turns are recorded in `meta.json` and are the unit of the cost ledger.

---

## 3. Sending a prompt

```python
async def send(self, session_id: str, prompt: str, wait_ms: int) -> SendResult:
    session = self.registry.get_or_raise(session_id)

    async with session.lock:
        self._assert_sendable(session)          # BUSY / TERMINAL / RATE_LIMITED / BUDGET
        turn = Turn(turn_id=new_ulid(), session_id=session_id, prompt=prompt,
                    started_at=utcnow(), start_cursor=session.event_log.cursor,
                    state="running")
        session.current_turn = turn
        session.transition(SessionState.RUNNING, reason="session_send")
        await session.client.query(prompt)      # returns as soon as it is written

    if wait_ms:
        await self._await_settled(session, turn, wait_ms)

    return self._snapshot(session, turn, since=turn.start_cursor)
```

`_await_settled` waits on `session.wakeup` (an `asyncio.Event` set by the consumer task) and returns
early when **any** of these becomes true:

- the turn reaches a terminal state,
- a permission request is parked,
- the session enters `cooldown` or `error`,
- `wait_ms` elapses.

Returning early on a parked permission is deliberate. A caller that long-polls for 60 s while the
session sits waiting on an approval that same caller must grant is a deadlock in slow motion.

---

## 4. Event normalisation

`normalise.py` maps SDK types to broker events. The mapping is total — an unrecognised message becomes
an `error` event with the raw payload in `details`, rather than being dropped. Silent drops here would
produce transcripts that disagree with reality.

```python
def normalise(msg: Message, idx: int) -> list[Event]:
    match msg:
        case SystemMessage(subtype="init", data=d):
            return [Event(idx, "session_init", model=d.get("model"),
                          tools=d.get("tools"), mcp_servers=d.get("mcp_servers"),
                          mcp_server_errors=d.get("mcp_server_errors", []),
                          capabilities=d.get("capabilities", []))]
        case SystemMessage(subtype="api_retry", data=d):
            return [Event(idx, "api_retry", attempt=d["attempt"],
                          max_retries=d["max_retries"],
                          retry_delay_ms=d["retry_delay_ms"], error=d["error"],
                          error_status=d.get("error_status"))]
        case AssistantMessage(content=blocks):
            return [_block_event(b, idx + i) for i, b in enumerate(blocks)]
        case ResultMessage() as r:
            return [Event(idx, "turn_result", is_error=r.is_error,
                          num_turns=r.num_turns, total_cost_usd=r.total_cost_usd,
                          usage=r.usage, model_usage=r.model_usage,
                          result=r.result, stop_reason=r.stop_reason,
                          permission_denials=r.permission_denials,
                          errors=r.errors, terminal_reason=r.terminal_reason)]
        case _:
            return [Event(idx, "error", code="UNMAPPED_MESSAGE",
                          message=type(msg).__name__, details=_safe_repr(msg))]
```

**Subagent attribution.** `parent_tool_use_id` is carried through onto `tool_use` and `assistant_text`
events. `None` means the main conversation; anything else is a subagent, and the value is the ID of the
`Agent` tool call that spawned it. Callers can rebuild the nesting tree from these IDs. The broker does
not set `--forward-subagent-text` by default — subagent prose is verbose and rarely what the caller
wants — but exposes it as the passthrough `extra_args` key `forward-subagent-text`.

---

## 5. Persistence

### 5.1 Event log

`events.jsonl` receives one JSON object per line, in cursor order, written on append. Recovery on
startup: for each session directory, read `meta.json`; if `state` is non-terminal, rewrite it to
`error` with `reason: "broker restarted"`. **Live sessions do not survive a broker restart** — the
subprocess dies with the container. What survives is the transcript and the SDK session state, so the
caller can `session_create(resume_from=...)` and continue.

This is worth stating plainly because it is the most likely operator surprise: *restarting the
container ends every running turn.* The recovery path is resume, not resurrect.

### 5.2 SDK session store

`FilesystemSessionStore` implements the SDK's `SessionStore` protocol against
`sessions/<id>/store/`. It is what makes `resume_from` work after a restart.

Model it on `examples/session_stores/` in the SDK repo — the three reference backends (Redis, Postgres,
S3) define the interface shape. Implement the same protocol; do not invent a different one, so that
swapping in Redis later is a config change.

`session_store_flush="batched"` is the default. Use `"immediate"` only if a test demonstrates data loss
under `"batched"`, because immediate flushing costs an fsync per message.

---

## 6. Concurrency rules

1. **One lock per session.** Every state read-modify-write takes it. Never hold it across an `await`
   on the model — only across the local transition.
2. **The consumer task never takes the session lock** for appends. `EventLog.append` is
   independently thread-safe; the consumer takes the lock only for state transitions, briefly.
3. **`session_poll` takes no lock.** It reads the event log and a state snapshot. Slightly stale reads
   are acceptable and preferable to poll traffic contending with turn execution.
4. **The reaper takes locks with `timeout=5`** and skips any session it cannot acquire, retrying next
   pass. A reaper that blocks on a busy session stalls all cleanup.
5. **Global limits** (`BROKER_MAX_SESSIONS`, global budget) are guarded by a single registry-level
   lock, taken before any session lock. Lock ordering is always registry → session, never the reverse.
