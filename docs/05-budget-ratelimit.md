# 05 — Budget & Rate Limits

The purpose of this subsystem is to make an autonomous agent's spend and throttling **visible and
bounded**, on a plan whose limits are opaque by design.

---

## 1. What you can and cannot measure

Be honest about the instrumentation available:

| Quantity | Observable? | How |
|---|---|---|
| Cost of a turn | ✔ | `ResultMessage.total_cost_usd` |
| Per-model breakdown | ✔ | `ResultMessage.model_usage` |
| Tokens in/out/cache | ✔ | `ResultMessage.usage` |
| Whether a request was throttled | ✔ | `SystemMessage(subtype="api_retry", error="rate_limit")` |
| **Remaining subscription quota** | ✖ | Not exposed to the CLI or SDK |
| **When the 5-hour window resets** | ✖ | Not exposed |

So the broker cannot implement a true quota manager. It implements two things it *can* do correctly:

1. **A cost ledger** — an accurate running total of what the plan was billed for, which is the leading
   indicator if Anthropic un-pauses the credit-pool change ([00 §1.1](00-decisions.md)).
2. **A reactive governor** — detects throttling the moment it happens and stops making it worse.

Do not fabricate a quota estimate from cost. `total_cost_usd` on a subscription is a notional API-rate
valuation, not a draw against a published dollar allowance. Presenting it as "quota used" would be
inventing a number.

---

## 2. Cost ledger

```python
@dataclass
class SessionCost:
    total_usd: float = 0.0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    by_model: dict[str, float] = field(default_factory=dict)
```

Updated only from `ResultMessage`. Never estimated mid-turn — a partial cost that later disagrees with
the authoritative total is worse than no number.

**Enforcement points**

| Ceiling | Where enforced | Behaviour on breach |
|---|---|---|
| `max_budget_usd` (per session) | SDK, natively | The SDK stops the run; `ResultMessage` carries a budget `stop_reason` |
| `BROKER_GLOBAL_BUDGET_USD` | Broker, pre-turn | `session_send` → `BUDGET_EXCEEDED` |

Per-session ceilings are delegated to the SDK because it can stop mid-turn; the broker can only refuse
to start one. Both layers are needed: the SDK bounds a single runaway turn, the broker bounds the
aggregate across sessions.

The global ledger persists to `ledger.jsonl`, one record per completed turn:

```jsonc
{"at": "2026-08-04T14:31:52Z", "session_id": "0f9c...", "turn_id": "t_01H...",
 "model": "claude-opus-5", "cost_usd": 0.184,
 "usage": {"input_tokens": 12043, "output_tokens": 1877,
           "cache_read_input_tokens": 98210, "cache_creation_input_tokens": 4102},
 "num_turns": 6, "duration_ms": 42817, "stop_reason": "end_turn"}
```

The global total is recomputed by replaying this file at startup, scoped to
`BROKER_BUDGET_WINDOW` (default `monthly`, calendar UTC). Replay rather than a checkpoint means the
ledger file is the single source of truth and cannot drift.

---

## 3. Rate-limit governor

### 3.1 Signal

Claude Code emits an `api_retry` system event before each retry:

| Field | Use |
|---|---|
| `error` | `rate_limit` is the trigger. Others are informational. |
| `attempt` / `max_retries` | Escalation — near-exhaustion is a stronger signal than attempt 1 |
| `retry_delay_ms` | The CLI's own backoff; a lower bound for ours |
| `error_status` | HTTP status, or `null` for connection errors |

Other `error` values worth distinguishing, because the correct response differs:

| `error` | Governor response |
|---|---|
| `rate_limit` | Enter cooldown |
| `overloaded` | Short cooldown (server-side capacity, not our quota) |
| `authentication_failed`, `oauth_org_not_allowed` | **Fail fast.** Do not retry. Mark broker `health: degraded`, surface `AUTH_FAILED`. Retrying a dead token just burns time. |
| `billing_error` | Fail fast, same reasoning |
| `server_error`, `unknown` | Log; no cooldown |

### 3.2 Cooldown

On `rate_limit`:

```
cooldown_seconds = min(
    BROKER_COOLDOWN_MAX,                                   # default 900
    max(retry_delay_ms / 1000, BROKER_COOLDOWN_BASE * 2 ** (consecutive_rate_limits - 1))
)
```

with `BROKER_COOLDOWN_BASE` default 30 s and `BROKER_COOLDOWN_MAX` default 900 s. Full jitter is
applied: the actual wait is `uniform(0, cooldown_seconds)` added to the floor of `retry_delay_ms`.
Jitter matters because several sessions throttled at once would otherwise retry in lockstep and
re-throttle together.

`consecutive_rate_limits` resets to 0 after any turn completes without a `rate_limit` retry.

**Scope: global, not per session.** Rate limits are an account-level property. Cooling down one session
while seven others hammer the same limit accomplishes nothing.

During cooldown:

- Live sessions transition to `cooldown`; in-flight turns are **not** interrupted — the CLI's own retry
  may well succeed, and killing a turn mid-way wastes everything it already spent.
- `session_send` returns `RATE_LIMITED` with `details.retry_after_seconds`.
- `session_create` is refused with the same error.
- `broker_status.rate_limit` reports `{state: "cooldown", cooldown_until, consecutive_rate_limits}`.

### 3.3 What the broker deliberately does not do

- **No preemptive throttling.** Without visibility into remaining quota, any preemptive rate is a
  guess that either wastes plan capacity or fails to prevent throttling.
- **No automatic model downgrade** on throttling. Silently switching Opus → Sonnet mid-task changes
  output quality without the caller's knowledge. Surface the throttle; let the caller decide.
- **No retry of a whole turn** after a rate-limited failure. The CLI already retries at the request
  level. Broker-level turn retry would double-spend on work already partially paid for.

---

## 4. Reporting

`broker_status.budget` is the operator's dashboard:

```jsonc
{
  "window": "monthly",
  "window_started_at": "2026-08-01T00:00:00Z",
  "global_spent_usd": 14.83,
  "global_limit_usd": 100.0,
  "turns": 212,
  "by_model": {"claude-opus-5": 12.40, "claude-sonnet-5": 2.43},
  "top_sessions": [{"session_id": "0f9c...", "label": "auth refactor", "spent_usd": 4.91}],
  "billing_mode": "subscription"
}
```

`billing_mode` mirrors `auth.billing`. **If it ever reads `api`, spend is real money on a
pay-as-you-go key rather than plan usage.** That flip is the thing this whole subsystem exists to make
impossible to miss: it forces `health: "degraded"`, logs at `ERROR` on every turn, and is reported on
every `broker_status` call.
