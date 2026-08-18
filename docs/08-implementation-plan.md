# 08 — Implementation Plan

Ordered tasks. Each is independently reviewable and ends in a working state. Do not reorder — later
tasks depend on earlier interfaces.

Every task follows: read the referenced spec section → write tests → implement → verify.

---

## Phase 1 — Foundation

### T01 · Project scaffold
**Spec:** [01 §4](01-architecture.md) · **Tests:** none (structural)

`pyproject.toml` with `claude-agent-sdk==0.2.129`, `mcp`, `pydantic`, `pyyaml`, `structlog`;
dev extras `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`. Create the full `src/broker/`
tree with stub modules. Ruff + mypy strict configured and passing on empty modules.

**Done when:** `pip install -e ".[dev]"` succeeds; `ruff check` and `mypy src/` pass.

### T02 · Config & preflight
**Spec:** [01 §6](01-architecture.md), [06 §3](06-docker-auth.md) · **Tests:** U1–U10

Frozen `Config` dataclass from env. All seven preflight checks. This is the billing guard — it lands
first so no later task can accidentally run against API billing.

**Done when:** U1–U10 pass. Manually verify: `ANTHROPIC_API_KEY=x python -m broker` exits non-zero
with a message naming the variable.

### T03 · Errors & workspace validation
**Spec:** [02 §4](02-mcp-tool-contract.md), [04 §2.2](04-permission-broker.md) · **Tests:** U11–U17

`BrokerError` hierarchy covering all 15 taxonomy codes, each with `code`, `retryable`, `details`.
`workspace.validate()` with full symlink resolution.

**Done when:** U11–U17 pass, including the symlink-escape case.

### T04 · Event log
**Spec:** [01 §2.3](01-architecture.md), [03 §4](03-session-broker.md) · **Tests:** U30–U36

`Event`, `EventLog`, cursor, JSONL sink, memory cap with disk read-through, `normalise()` with total
mapping.

**Done when:** U30–U36 pass; U33 verifies disk read-through returns identical content.

---

## Phase 2 — Core broker

### T05 · Fake SDK client
**Spec:** [07 §1](07-testing.md) · **Tests:** self-testing fixture

Build this before the session manager. Everything in Phase 2 and 3 is tested through it, so its
fidelity sets the ceiling on test quality. Must support scripted sequences, injected delays,
mid-stream exceptions, blocking-until-released, and invoking `can_use_tool`.

**Done when:** the fixture can drive a full scripted turn including a permission callback.

### T06 · Session registry & state machine
**Spec:** [03 §1–2](03-session-broker.md) · **Tests:** I1–I10

`Session`, `SessionState`, `Turn`, `SessionRegistry`, transitions with invariant assertions, per-session
locks, registry→session lock ordering.

**Done when:** I1–I10 pass; all four invariants assert in a debug build.

### T07 · Consumer task & session creation
**Spec:** [01 §3](01-architecture.md), [02 §1.1](02-mcp-tool-contract.md) · **Tests:** I1, I9, I10

Wire `ClaudeSDKClient` with the full `ClaudeAgentOptions` mapping. Start the consumer task. Handle
`system/init`, including `mcp_server_errors`.

> **Do not** iterate `receive_response()` inside a request handler. The consumer task owns the stream
> for the session's whole lifetime, or interrupts will not work. This is the design's sharpest edge.

**Done when:** a session reaches `idle` after a scripted `init`; I9 shows a clean `error` transition.

### T08 · Send & poll
**Spec:** [02 §1.2–1.3](02-mcp-tool-contract.md), [03 §3](03-session-broker.md) · **Tests:** I11–I16

`send()` with `wait_ms` long-poll; `poll()` with cursor, limit, filter. Early return on settle, parked
permission, cooldown, or error.

**Done when:** I11–I16 pass. I14 (early return on parked permission) is the anti-deadlock test.

### T09 · Interrupt
**Spec:** [02 §1.4](02-mcp-tool-contract.md) · **Tests:** I32–I36

**Done when:** I32–I36 pass, each with an explicit 15 s timeout so a hang fails rather than stalls.

---

## Phase 3 — Policy & accounting

### T10 · Policy engine
**Spec:** [04 §2](04-permission-broker.md) · **Tests:** U18–U29

YAML loader, ordered matching with Claude Code rule syntax, shadowing detection, the three guards. Ship
`policies/default.yaml` with all three policies.

**Done when:** U18–U29 pass. U19 (space significance) and U21 (shadow rejection) are the subtle ones.

### T11 · Permission broker
**Spec:** [04 §3–5](04-permission-broker.md) · **Tests:** I17–I31

`can_use_tool`, park-and-resolve on `asyncio.Event`, timeout-to-deny, session overlay, audit log.

**Done when:** I17–I31 pass; `permissions.jsonl` records allows as well as denies.

### T12 · Cost ledger
**Spec:** [05 §2](05-budget-ratelimit.md) · **Tests:** U37–U42

**Done when:** U37–U42 pass; U40/U41 confirm replay and window scoping.

### T13 · Rate-limit governor
**Spec:** [05 §3](05-budget-ratelimit.md) · **Tests:** U43–U51

Per-`error`-value dispatch, exponential backoff with full jitter, global cooldown, fail-fast on auth
and billing errors.

**Done when:** U43–U51 pass; U45 confirms no retry storm against a dead token.

### T14 · Session store & persistence
**Spec:** [03 §5](03-session-broker.md) · **Tests:** I42–I46

`FilesystemSessionStore` against the SDK `SessionStore` protocol — model the interface on
`examples/session_stores/` so a Redis swap is a config change. Restart recovery. Corrupt-tail
tolerance.

**Done when:** I42–I46 pass; I46 confirms a truncated final line does not crash startup.

### T15 · Reaper
**Spec:** [01 §2.6](01-architecture.md) · **Tests:** extend I6

Idle TTL, terminal retention, expired-permission sweep. Lock acquisition with `timeout=5` and skip.

**Done when:** an idle session past TTL is closed; a busy session is skipped, not blocked on.

---

## Phase 4 — Surface

### T16 · MCP facade
**Spec:** [02](02-mcp-tool-contract.md) in full · **Tests:** I47–I53

All 12 tools with strict schemas, bearer auth with `compare_digest`, error mapping, no stack-trace
leakage.

**Done when:** I47–I53 pass; every tool's schema validates and rejects unknown fields.

### T17 · Fork, resume, transcript, list
**Spec:** [02 §1.5, 1.7, 1.8](02-mcp-tool-contract.md) · **Tests:** I37–I41

**Done when:** I37–I41 pass; I38 confirms forking a busy session is refused.

### T18 · Status & workspace discovery
**Spec:** [02 §3](02-mcp-tool-contract.md), [05 §4](05-budget-ratelimit.md) · **Tests:** I53

`broker_status` including the computed `auth.billing`, token expiry decode, and `health: degraded` on
API billing. `workspace_list`.

**Done when:** I53 passes; `broker_status` reports `subscription` under a real token.

---

## Phase 5 — Ship

### T19 · Docker image
**Spec:** [06 §2](06-docker-auth.md)

Multi-stage build, pinned CLI, non-root, tini, healthcheck, `CLAUDE_CONFIG_DIR` on the state volume.

**Done when:** `docker build` succeeds; the container starts, preflights, and serves `broker_status`.

### T20 · Compose & hardening
**Spec:** [06 §4–6](06-docker-auth.md)

Secrets as files, read-only root, `cap_drop: ALL`, `no-new-privileges`, pids and memory limits,
loopback port binding.

**Done when:** the stack runs read-only end to end; a write outside the declared volumes fails.

### T21 · Live validation
**Spec:** [07 §4](07-testing.md) · **Tests:** L1–L10

**Done when:** all ten pass against a real token. **L8 gates release** — it is the only proof the
budget ceiling binds.

### T22 · Operator docs
Quickstart, `.env.example`, policy authoring guide, troubleshooting (expired token, API-key hijack,
Windows bind-mount performance), annual token-renewal runbook.

**Done when:** someone who has not read these specs can go from `git clone` to a working session.

---

## Sequencing

```
T01 ─▶ T02 ─▶ T03 ─▶ T04 ─▶ T05 ─▶ T06 ─▶ T07 ─▶ T08 ─▶ T09
                                                          │
                        ┌─────────────────────────────────┤
                        ▼                                 ▼
                  T10 ─▶ T11                      T12 ─▶ T13
                        │                                 │
                        └────────────┬────────────────────┘
                                     ▼
                              T14 ─▶ T15 ─▶ T16 ─▶ T17 ─▶ T18
                                                            │
                                              T19 ─▶ T20 ─▶ T21 ─▶ T22
```

T10–T11 (policy) and T12–T13 (accounting) are independent of each other after T09 and can proceed in
parallel if two implementers are available.

**Review checkpoints:** after T04, T09, T13, T18, T21. Each is a coherent, demonstrable increment.

---

## Estimation

| Phase | Tasks | Rough effort |
|---|---|---|
| 1 Foundation | T01–T04 | 1–1.5 days |
| 2 Core broker | T05–T09 | 2–3 days |
| 3 Policy & accounting | T10–T15 | 2–3 days |
| 4 Surface | T16–T18 | 1.5–2 days |
| 5 Ship | T19–T22 | 1.5–2 days |
| | | **~9–12 days** |

Assumes one implementer working from these specs without further design decisions. The riskiest task is
**T07/T09** — the consumer-task pattern is easy to get subtly wrong, and the failure mode is a hang
rather than an exception.

---

## Definition of done

- [ ] `pytest -m "not live"` green; coverage gates in [07 §5](07-testing.md) met
- [ ] `pytest -m live` green, including L8
- [ ] `ruff check` and `mypy --strict src/` clean
- [ ] Container runs read-only, non-root, with dropped capabilities
- [ ] `broker_status` reports `billing: "subscription"` against a real token
- [ ] Preflight refuses to start with `ANTHROPIC_API_KEY` set
- [ ] A 30-minute autonomous run completes without a client timeout, with a full transcript and audit
      log on disk
