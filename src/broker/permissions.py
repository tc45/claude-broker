"""Permission policy engine and broker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml

from broker.errors import PermissionExpiredError, PermissionNotFoundError
from broker.event_log import Event
from broker.registry import Session, SessionState

logger = structlog.get_logger()


def utcnow() -> datetime:
	return datetime.now(tz=UTC)


class PolicyLoadError(Exception):
	pass


@dataclass
class RuleDecision:
	verdict: Literal["allow", "deny", "ask"]
	reason: str | None = None
	rule_text: str | None = None
	decided_by: str = "policy"
	guard: str | None = None
	updated_input: dict[str, Any] | None = None
	interrupt: bool = False


@dataclass
class PolicyRule:
	match: str
	decision: Literal["allow", "deny", "ask"]
	reason: str | None = None
	guard: str | None = None

	def rule_text(self) -> str:
		return f"{self.decision}: {self.match}"


@dataclass
class Policy:
	name: str
	description: str
	default: Literal["allow", "deny", "ask"]
	rules: list[PolicyRule] = field(default_factory=list)
	overlay_rules: list[PolicyRule] = field(default_factory=list)

	def evaluate(
		self,
		tool_name: str,
		input_data: dict[str, Any],
		session: Session | None = None,
	) -> RuleDecision:
		for rule in self.overlay_rules + self.rules:
			if _matches(rule.match, tool_name, input_data):
				decision = RuleDecision(
					verdict=rule.decision,
					reason=rule.reason,
					rule_text=rule.rule_text(),
					guard=rule.guard,
				)
				if rule.guard and session:
					decision = _apply_guard(decision, rule.guard, tool_name, input_data, session)
				return decision
		return RuleDecision(verdict=self.default, rule_text=f"default: {self.default}")


def _matches(pattern: str, tool_name: str, input_data: dict[str, Any]) -> bool:
	if "(" in pattern:
		tool_part, rest = pattern.split("(", 1)
		prefix = rest.rstrip(")")
		if tool_part != tool_name:
			return False
		value = _extract_match_value(tool_name, input_data)
		if prefix.endswith(" *"):
			return value.startswith(prefix[:-2] + " ")
		if prefix.endswith("*"):
			return value.startswith(prefix[:-1])
		return value == prefix
	if pattern.endswith("*"):
		return tool_name.startswith(pattern[:-1])
	return pattern == tool_name


def _extract_match_value(tool_name: str, input_data: dict[str, Any]) -> str:
	if tool_name == "Bash":
		return str(input_data.get("command", ""))
	if tool_name in ("Edit", "Write", "Read"):
		return str(input_data.get("file_path", ""))
	return json.dumps(input_data, sort_keys=True)


def _apply_guard(
	decision: RuleDecision,
	guard: str,
	tool_name: str,
	input_data: dict[str, Any],
	session: Session,
) -> RuleDecision:
	if decision.verdict != "allow":
		return decision
	if guard == "workspace_write":
		allowed_roots = [session.workspace, *(Path(p) for p in session.additional_directories)]
		for path_str in _extract_paths(tool_name, input_data):
			try:
				target = _resolve_target(path_str, session.workspace)
			except OSError:
				return RuleDecision(
					verdict="deny",
					reason=f"{tool_name} target {path_str!r} is not a usable path.",
					rule_text=decision.rule_text,
					decided_by="guard",
					guard=guard,
				)
			if not _confined(target, allowed_roots):
				roots = ", ".join(str(r) for r in allowed_roots)
				# The reason is the only thing the model sees, and the only thing an
				# observer sees in the permission_decision event. Name the path.
				return RuleDecision(
					verdict="deny",
					reason=(
						f"{tool_name} to {path_str!r} resolves to {target}, which is "
						f"outside the session workspace ({roots}). "
						f"Use a path inside the workspace instead."
					),
					rule_text=decision.rule_text,
					decided_by="guard",
					guard=guard,
				)
	elif guard == "no_push":
		cmd = str(input_data.get("command", ""))
		if re.search(r"git\s+push", cmd) or re.search(r"git\s+remote\s+add", cmd):
			return RuleDecision(
				verdict="ask",
				reason="Push operations require approval",
				rule_text=decision.rule_text,
				decided_by="guard",
				guard=guard,
			)
		if re.search(r"git\s+config", cmd):
			return RuleDecision(
				verdict="ask",
				reason="Git config changes require approval",
				decided_by="guard",
				guard=guard,
			)
	elif guard == "no_secrets":
		path = str(input_data.get("file_path", input_data.get("command", "")))
		secret_patterns = [".env", ".pem", ".key", ".credentials.json", "id_rsa"]
		for pat in secret_patterns:
			if pat in path:
				return RuleDecision(
					verdict="ask",
					reason="Potential secret access",
					decided_by="guard",
					guard=guard,
				)
	return decision


def _resolve_target(path_str: str, workspace: Path) -> Path:
	"""Resolve a tool path the way the tool itself will see it.

	The CLI normally sends absolute paths, but a model can hand Write a bare
	filename. That resolves against the CLI's cwd — the session workspace — not
	against the broker process's cwd, which is where a plain Path().resolve()
	would put it and which would judge an in-workspace write against the wrong
	root.
	"""
	target = Path(path_str)
	if not target.is_absolute():
		target = workspace / target
	# strict=False: the file usually does not exist yet, but symlinks in the
	# existing prefix still get resolved, so an escape via a symlinked dir is
	# caught here rather than at write time.
	return target.resolve()


def _confined(target: Path, roots: list[Path]) -> bool:
	for root in roots:
		try:
			target.relative_to(root.resolve())
			return True
		except (OSError, ValueError):
			continue
	return False


def _extract_paths(tool_name: str, input_data: dict[str, Any]) -> list[str]:
	if tool_name in ("Edit", "Write", "Read"):
		fp = input_data.get("file_path")
		return [str(fp)] if fp else []
	return []


def _rule_specificity(rule: PolicyRule) -> tuple[int, int]:
	match = rule.match
	if "(" in match:
		prefix = match.split("(", 1)[1].rstrip(")")
		return (1, -len(prefix))
	return (0, -len(match))


def _detect_shadowing(rules: list[PolicyRule]) -> None:
	for i, general in enumerate(rules):
		for specific in rules[i + 1 :]:
			if _would_shadow(general.match, specific.match):
				raise PolicyLoadError(
					f"Rule {general.rule_text()} shadows later specific rule {specific.rule_text()}"
				)


def _would_shadow(general: str, specific: str) -> bool:
	gen_tool = general.split("(", 1)[0] if "(" in general else general.replace("*", "")
	spec_tool = specific.split("(", 1)[0] if "(" in specific else specific.replace("*", "")
	if gen_tool != spec_tool and not general.endswith("*"):
		return False
	if "(" not in general and "(" in specific:
		return general == spec_tool or general.endswith("*")
	return False


class PolicyEngine:
	"""Loads and provides named policies from YAML."""

	def __init__(self, policy_file: Path) -> None:
		self._policies: dict[str, Policy] = {}
		self._load(policy_file)

	def _load(self, path: Path) -> None:
		with open(path, encoding="utf-8") as f:
			data = yaml.safe_load(f)
		for name, spec in data.get("policies", {}).items():
			rules = [
				PolicyRule(
					match=r["match"],
					decision=r["decision"],
					reason=r.get("reason"),
					guard=r.get("guard"),
				)
				for r in spec.get("rules", [])
			]
			_detect_shadowing(rules)
			self._policies[name] = Policy(
				name=name,
				description=spec.get("description", ""),
				default=spec.get("default", "ask"),
				rules=rules,
			)

	def get(self, name: str) -> Policy:
		if name not in self._policies:
			raise KeyError(f"Unknown policy: {name}")
		return self._policies[name]

	def list_names(self) -> list[str]:
		return list(self._policies.keys())


@dataclass
class PendingRequest:
	request_id: str
	session_id: str
	tool: str
	input: dict[str, Any]
	matched_rule: str | None
	suggestions: list[Any]
	requested_at: datetime
	expires_at: datetime
	decision: RuleDecision | None = None
	resolved: asyncio.Event = field(default_factory=asyncio.Event)


class PermissionBroker:
	"""Implements can_use_tool with park-and-resolve semantics."""

	def __init__(
		self,
		registry: Any,
		policy_engine: PolicyEngine,
		timeout: int = 300,
		state_dir: Path | None = None,
	) -> None:
		self._registry = registry
		self._policy_engine = policy_engine
		self._timeout = timeout
		self._pending: dict[str, PendingRequest] = {}
		self._seq = 0
		self._state_dir = state_dir

	def _next_id(self) -> str:
		self._seq += 1
		return f"p_{self._seq:03d}"

	def get_policy(self, name: str) -> Policy:
		return self._policy_engine.get(name)

	def audit(
		self,
		session_id: str,
		tool: str,
		input_data: dict[str, Any],
		decision: RuleDecision,
		*,
		turn_id: str | None = None,
		request_id: str | None = None,
		decided_by: str | None = None,
		latency_ms: int = 0,
		remembered: bool = False,
	) -> None:
		if not self._state_dir:
			return
		session_dir = self._state_dir / "sessions" / session_id
		session_dir.mkdir(parents=True, exist_ok=True)
		audit_path = session_dir / "permissions.jsonl"
		input_str = json.dumps(input_data, sort_keys=True, default=str)
		digest = hashlib.sha256(input_str.encode()).hexdigest()
		record = {
			"at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
			"session_id": session_id,
			"turn_id": turn_id,
			"request_id": request_id,
			"tool": tool,
			"input_digest": f"sha256:{digest}",
			"input_preview": input_str[:256],
			"matched_rule": decision.rule_text,
			"guard": decision.guard,
			"verdict": decision.verdict,
			"decided_by": decided_by or decision.decided_by,
			"latency_ms": latency_ms,
			"reason": decision.reason,
			"remembered": remembered,
		}
		with open(audit_path, "a", encoding="utf-8") as f:
			f.write(json.dumps(record) + "\n")

	def _emit_decision(
		self,
		session: Session,
		tool: str,
		input_data: dict[str, Any],
		decision: RuleDecision,
	) -> None:
		"""Record a decision taken without parking, so an observer can see it.

		A policy or guard deny is answered straight back to the SDK and never
		becomes a PendingRequest, so nothing used to reach the event log: the run
		stalled (the model usually falls back to a tool that *does* need approval)
		and the transcript showed a tool_use with no outcome. Poll-only observers
		had literally nothing to explain the stall.
		"""
		session.event_log.append(
			Event.immediate_decision(session.event_log.cursor, tool, input_data, decision)
		)
		session.wakeup.set()

	def make_callback(self, session_id: str, policy: Policy) -> Any:
		async def can_use_tool(
			tool_name: str,
			input_data: dict[str, Any],
			context: Any = None,
		) -> Any:
			from claude_agent_sdk import PermissionResultDeny

			session = self._registry.get(session_id)
			if session is None:
				return PermissionResultDeny(message="Session not found")
			try:
				return await decide(session, tool_name, input_data, context)
			except Exception as exc:
				# An exception escaping into the SDK is answered as a
				# control-protocol *error*, not a permission result: the CLI never
				# produces a tool_result, the turn never ends, and the session sits
				# in RUNNING with nothing in the log to say why. Fail closed and
				# visibly instead — the model can then pick another path.
				logger.exception(
					"permission_callback_failed", session_id=session_id, tool=tool_name
				)
				reason = f"Permission check failed inside the broker: {exc!r}"
				failure = RuleDecision(
					verdict="deny", reason=reason, decided_by="broker_error"
				)
				try:
					self.audit(session_id, tool_name, input_data, failure)
					self._emit_decision(session, tool_name, input_data, failure)
				except Exception:  # pragma: no cover - logging must not mask the deny
					logger.exception("permission_failure_not_recorded", session_id=session_id)
				return PermissionResultDeny(message=reason)

		async def decide(
			session: Session,
			tool_name: str,
			input_data: dict[str, Any],
			context: Any,
		) -> Any:
			from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

			session_id = session.session_id
			start = utcnow()
			decision = policy.evaluate(tool_name, input_data, session)
			self.audit(
				session_id,
				tool_name,
				input_data,
				decision,
				turn_id=session.current_turn.turn_id if session.current_turn else None,
				decided_by=decision.decided_by,
			)

			if decision.verdict == "allow":
				return PermissionResultAllow()

			if decision.verdict == "deny":
				reason = decision.reason or f"Denied by policy ({decision.rule_text})"
				decision.reason = reason
				self._emit_decision(session, tool_name, input_data, decision)
				return PermissionResultDeny(message=reason)

			suggestions = getattr(context, "suggestions", []) if context else []
			request = PendingRequest(
				request_id=self._next_id(),
				session_id=session_id,
				tool=tool_name,
				input=input_data,
				matched_rule=decision.rule_text,
				suggestions=suggestions,
				requested_at=utcnow(),
				expires_at=utcnow() + timedelta(seconds=self._timeout),
			)
			self._pending[request.request_id] = request
			if session.current_turn:
				session.current_turn.permission_requests.append(request.request_id)
			session.event_log.append(
				Event.permission_request(session.event_log.cursor, request)
			)
			async with session.lock:
				session.transition(SessionState.AWAITING_PERMISSION, reason=request.request_id)
			session.wakeup.set()

			try:
				await asyncio.wait_for(request.resolved.wait(), timeout=self._timeout)
			except TimeoutError:
				request.decision = RuleDecision(
					verdict="deny",
					reason=f"No response within {self._timeout}s; denied by default.",
					decided_by="timeout",
				)
			finally:
				self._pending.pop(request.request_id, None)
				async with session.lock:
					if session.state == SessionState.AWAITING_PERMISSION:
						session.transition(SessionState.RUNNING, reason="permission resolved")

			if not request.decision:
				request.decision = RuleDecision(
					verdict="deny",
					reason="No decision",
					decided_by="timeout",
				)

			latency = int((utcnow() - start).total_seconds() * 1000)
			self.audit(
				session_id,
				tool_name,
				input_data,
				request.decision,
				turn_id=session.current_turn.turn_id if session.current_turn else None,
				request_id=request.request_id,
				decided_by=request.decision.decided_by,
				latency_ms=latency,
			)
			session.event_log.append(
				Event.permission_decision(session.event_log.cursor, request)
			)
			session.wakeup.set()

			if request.decision.verdict == "allow":
				return PermissionResultAllow(updated_input=request.decision.updated_input)
			return PermissionResultDeny(
				message=request.decision.reason or "Denied",
				interrupt=request.decision.interrupt,
			)

		return can_use_tool

	def list_pending(self, session_id: str | None = None) -> list[dict[str, Any]]:
		result = []
		for req in self._pending.values():
			if session_id and req.session_id != session_id:
				continue
			result.append({
				"request_id": req.request_id,
				"session_id": req.session_id,
				"tool": req.tool,
				"input": req.input,
				"matched_rule": req.matched_rule,
				"suggestions": req.suggestions,
				"requested_at": req.requested_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
				"expires_at": req.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
			})
		return result

	def resolve(
		self,
		request_id: str,
		decision: str,
		reason: str | None = None,
		updated_input: dict[str, Any] | None = None,
		interrupt: bool = False,
		remember: str = "none",
	) -> dict[str, Any]:
		req = self._pending.get(request_id)
		if req is None or req.resolved.is_set():
			raise PermissionNotFoundError(request_id)
		if utcnow() > req.expires_at:
			raise PermissionExpiredError(request_id)

		session = self._registry.get(req.session_id)
		decided_by = "mcp_client"
		req.decision = RuleDecision(
			verdict=decision,  # type: ignore[arg-type]
			reason=reason,
			decided_by=decided_by,
			updated_input=updated_input,
			interrupt=interrupt,
		)

		if remember == "session" and session and decision == "allow":
			policy_name = session.permission_policy
			policy = self._policy_engine.get(policy_name)
			match_str = req.tool
			if req.tool == "Bash" and "command" in req.input:
				cmd = req.input["command"]
				match_str = f"Bash({cmd})"
			policy.overlay_rules.insert(
				0,
				PolicyRule(match=match_str, decision="allow"),
			)

		req.resolved.set()
		state = session.state.value if session else "unknown"
		return {
			"request_id": request_id,
			"session_id": req.session_id,
			"decision": decision,
			"applied": True,
			"state": state,
		}

	def deny_all_for_turn(self, session: Session) -> None:
		"""Resolve all pending requests for current turn as deny."""
		if not session.current_turn:
			return
		for rid in list(session.current_turn.permission_requests):
			req = self._pending.get(rid)
			if req and not req.resolved.is_set():
				req.decision = RuleDecision(
					verdict="deny",
					reason="Turn ended",
					decided_by="timeout",
				)
				req.resolved.set()

	def sweep_expired(self) -> None:
		now = utcnow()
		for req in list(self._pending.values()):
			if now > req.expires_at and not req.resolved.is_set():
				req.decision = RuleDecision(
					verdict="deny",
					reason="Expired",
					decided_by="timeout",
				)
				req.resolved.set()
