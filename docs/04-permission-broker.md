# 04 — Permission Broker

The single biggest departure from the archived reference project, which defaulted to
`--dangerously-skip-permissions`.

---

## 1. Principle

The broker **never** passes `--dangerously-skip-permissions` or
`permission_mode="bypassPermissions"`. `permission_mode` is pinned to `"default"` so that
`can_use_tool` is consulted for every tool call that is not already covered by an `allowed_tools` rule.

Bypass-equivalent behaviour is still available via the `autonomous` policy — but expressed as policy,
it is logged, attributable per tool call, and revocable per session, rather than being an invisible
process-wide flag.

Two layers, and they are not interchangeable:

| Layer | Enforces | Defeated by |
|---|---|---|
| Permission policy | Intent | Prompt injection that stays within allowed rules |
| Container | Capability | Nothing short of a container escape |

Policy is not containment. See [ADR-005](00-decisions.md).

---

## 2. Policy model

A policy is an ordered rule list. First match wins. No match falls through to the policy's `default`.

```yaml
# policies/default.yaml
policies:

  readonly:
    description: Inspection only. Cannot mutate the workspace.
    default: deny
    rules:
      - {match: "Read",          decision: allow}
      - {match: "Glob",          decision: allow}
      - {match: "Grep",          decision: allow}
      - {match: "NotebookRead",  decision: allow}
      - {match: "Bash(git log *)",    decision: allow}
      - {match: "Bash(git diff *)",   decision: allow}
      - {match: "Bash(git status *)", decision: allow}
      - {match: "WebFetch",      decision: ask}
      - {match: "WebSearch",     decision: ask}

  reviewed:
    description: Default. Reads and edits freely; asks before shell and network.
    default: ask
    rules:
      - {match: "Read",   decision: allow}
      - {match: "Glob",   decision: allow}
      - {match: "Grep",   decision: allow}
      - {match: "Edit",   decision: allow, guard: workspace_write}
      - {match: "Write",  decision: allow, guard: workspace_write}
      - {match: "TodoWrite", decision: allow}
      - {match: "Bash(git *)",   decision: allow, guard: no_push}
      - {match: "Bash(npm test*)",   decision: allow}
      - {match: "Bash(pytest *)",    decision: allow}
      - {match: "Bash(rm *)",    decision: deny, reason: "Deletion requires an explicit policy."}
      - {match: "Bash(sudo *)",  decision: deny, reason: "Privilege escalation is never permitted."}
      - {match: "Bash(curl *)",  decision: ask}
      - {match: "Bash",          decision: ask}
      - {match: "mcp__*",        decision: ask}

  autonomous:
    description: >
      Unattended operation. Allows everything except a hard deny-list.
      Only safe behind a hardened container with restricted egress.
    default: allow
    rules:
      - {match: "Bash(sudo *)",       decision: deny, reason: "Privilege escalation."}
      - {match: "Bash(rm -rf /*)",    decision: deny, reason: "Catastrophic deletion."}
      - {match: "Bash(chmod 777 *)",  decision: deny, reason: "Permission widening."}
      - {match: "Bash(* --force *)",  decision: ask}
      - {match: "Bash(git push *)",   decision: ask}
      - {match: "Edit",  decision: allow, guard: workspace_write}
      - {match: "Write", decision: allow, guard: workspace_write}
```

### 2.1 Match syntax

Reuses Claude Code's permission-rule syntax, so operators do not learn a second dialect:

- `Read` — the tool, any input.
- `Bash(git diff *)` — prefix match. **The space before `*` is significant**: `Bash(git diff*)` would
  also match `git diff-index`.
- `mcp__server__tool` — a specific MCP tool; `mcp__*` matches all.

Rules are evaluated top to bottom. Put specific rules above general ones — `Bash(rm *)` must precede
`Bash`, or the general rule shadows it. **The policy loader must reject a file where a general rule
shadows a later specific one**, rather than silently applying it. Shadowed deny rules are exactly the
kind of bug that is invisible until it matters.

### 2.2 Guards

Guards run after a rule matches and can downgrade `allow` to `deny` or `ask`. They express constraints
that pattern matching cannot.

| Guard | Rejects |
|---|---|
| `workspace_write` | Any path resolving outside the session's `workspace` + `additional_directories`, after full symlink resolution. Downgrades to `deny`. |
| `no_push` | `git push`, `git remote add`, `git config` writes. Downgrades to `ask`. |
| `no_secrets` | Reads of `.env`, `*.pem`, `*.key`, `.credentials.json`, `id_rsa*`. Downgrades to `ask`. |

`workspace_write` is the one that earns its keep. `Edit`/`Write` inputs carry a `file_path`; the guard
resolves it (including `..` and symlinks) and confirms containment. Path traversal via a symlink
planted inside the workspace is otherwise a clean escape from an apparently-scoped policy.

---

## 3. The callback

```python
def make_callback(self, session_id: str, policy: Policy) -> CanUseTool:

    async def can_use_tool(
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:

        session = self.registry.get(session_id)
        decision = policy.evaluate(tool_name, input_data, session)   # allow | deny | ask

        self.audit(session_id, tool_name, input_data, decision)

        if decision.verdict == "allow":
            return PermissionResultAllow()

        if decision.verdict == "deny":
            return PermissionResultDeny(message=decision.reason)

        # ask -> park
        request = PendingRequest(
            request_id=f"p_{next_seq()}",
            session_id=session_id,
            tool=tool_name,
            input=input_data,
            matched_rule=decision.rule_text,
            suggestions=context.suggestions,
            requested_at=utcnow(),
            expires_at=utcnow() + timedelta(seconds=self.timeout),
        )
        self.pending[request.request_id] = request
        session.event_log.append(Event.permission_request(request))
        session.transition(SessionState.AWAITING_PERMISSION, reason=request.request_id)
        session.wakeup.set()

        try:
            await asyncio.wait_for(request.resolved.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            request.decision = Decision.deny(
                reason=f"No response within {self.timeout}s; denied by default.")
        finally:
            self.pending.pop(request.request_id, None)
            session.transition(SessionState.RUNNING, reason="permission resolved")

        session.event_log.append(Event.permission_decision(request))

        if request.decision.verdict == "allow":
            return PermissionResultAllow(updated_input=request.decision.updated_input)
        return PermissionResultDeny(
            message=request.decision.reason,
            interrupt=request.decision.interrupt,
        )

    return can_use_tool
```

**Design notes**

- The callback blocks inside the SDK's permission flow. That is correct and intended — the child
  session genuinely is waiting. It does not block the broker: the consumer task and every other
  session run independently.
- Timeout **denies**. Fail closed. An unattended broker whose operator went to lunch must not approve
  a `git push` by inaction.
- `context.suggestions` is forwarded verbatim to the caller. These are Claude's own proposed permission
  updates, and letting a supervising agent accept them turns a repetitive approval loop into one
  decision.
- Every evaluation is audited, including `allow`. The audit log is the answer to "what did the
  autonomous session actually do at 3am," and it must not be sampled.

---

## 4. Session overlay

`permission_resolve(remember="session")` prepends a rule to an in-memory overlay consulted before the
base policy. Overlay rules:

- last for the session only,
- are dropped on `session_close`,
- are **not** inherited by forks (a fork gets the base policy; inheriting ad-hoc grants across a branch
  is how privilege quietly accumulates),
- are never written to the policy file.

Durable policy change is an operator action: edit the YAML, restart the broker.

---

## 5. Audit record

Appended to `sessions/<id>/permissions.jsonl`:

```jsonc
{"at": "2026-08-04T14:24:10Z", "session_id": "0f9c...", "turn_id": "t_01H...",
 "request_id": "p_03", "tool": "Bash", "input_digest": "sha256:9f2c...",
 "input_preview": "pytest -q", "matched_rule": "ask: Bash", "guard": null,
 "verdict": "allow", "decided_by": "mcp_client", "latency_ms": 1840,
 "reason": null, "remembered": false}
```

`input_digest` is a full-input hash; `input_preview` is truncated to 256 chars. Hashing the full input
lets you prove after the fact exactly what was approved without storing potentially large or sensitive
payloads twice.

`decided_by` ∈ `policy` | `guard` | `mcp_client` | `timeout` | `interrupt` | `broker_error`.

A decision the policy or a guard makes on its own is answered straight to the SDK — no request is
parked, so there is no `request_id`. It still emits a `permission_decision` event, because otherwise a
denial is invisible: the transcript shows a `tool_use` with no outcome and an observer has nothing to
explain why the run then stalled on whatever the model reached for next.

`broker_error` is the fail-closed path: any exception inside `can_use_tool` becomes a deny with the
error text as its reason. Letting it escape instead makes the SDK answer the CLI's permission request
with a control-protocol *error*, and the CLI then never produces a `tool_result` — the turn never ends
and the session sits in `running` forever.
