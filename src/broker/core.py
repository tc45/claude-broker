"""Broker core orchestrating sessions, events, permissions, and budget."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from broker.budget import CostLedger, RateLimitGovernor, round_usd
from broker.config import Config, decode_token_expiry, get_auth_probe_result, probe_cli_version
from broker.errors import (
	InternalError,
	InterruptTimeoutError,
	McpServerFailedError,
	RateLimitedError,
	SessionBusyError,
	SessionLimitReachedError,
	SessionNotFoundError,
	SessionTerminalError,
)
from broker.event_log import Event, EventLog, format_ts
from broker.normalise import normalise
from broker.permissions import PermissionBroker, PolicyEngine
from broker.registry import Session, SessionRegistry, SessionState, Turn, new_ulid, utcnow
from broker.session_store import FilesystemSessionStore, SessionPersistence
from broker.workspace import validate as validate_workspace

logger = structlog.get_logger()

STARTUP_TIME = datetime.now(tz=UTC)


class BrokerCore:
	"""Central broker orchestrator."""

	def __init__(
		self,
		config: Config,
		*,
		client_factory: Any | None = None,
	) -> None:
		self.config = config
		self.registry = SessionRegistry(config.max_sessions)
		self.persistence = SessionPersistence(config.state_dir)
		self.policy_engine = PolicyEngine(config.policy_file)
		self.permissions = PermissionBroker(
			self.registry,
			self.policy_engine,
			timeout=config.permission_timeout,
			state_dir=config.state_dir,
		)
		self.ledger = CostLedger(
			config.state_dir,
			global_budget_usd=config.global_budget_usd,
			budget_window=config.budget_window,
		)
		self.governor = RateLimitGovernor(
			cooldown_base=config.cooldown_base,
			cooldown_max=config.cooldown_max,
		)
		self._client_factory = client_factory
		self._closed_sessions: dict[str, dict[str, Any]] = {}
		self.persistence.recover_on_startup()

	async def _consume(self, session: Session) -> None:
		"""Owns the message stream for a session's entire lifetime."""
		try:
			async for message in session.client.receive_messages():
				events = normalise(message, session.event_log.cursor)
				session.event_log.append(events)
				await self._apply_side_effects(session, message, events)
				session.wakeup.set()
		except asyncio.CancelledError:
			raise
		except Exception as exc:
			async with session.lock:
				session.transition(SessionState.ERROR, reason=repr(exc))
			session.wakeup.set()
		else:
			# Stream ended (e.g. interrupt closed the iterator)
			async with session.lock:
				if session.state in (SessionState.RUNNING, SessionState.AWAITING_PERMISSION):
					if session.current_turn:
						session.current_turn.state = "interrupted"
					session.current_turn = None
					session.transition(SessionState.IDLE, reason="stream ended")
			session.wakeup.set()

	async def _apply_side_effects(
		self,
		session: Session,
		message: Any,
		events: list[Event],
	) -> None:
		msg_type = type(message).__name__

		if msg_type == "SystemMessage":
			subtype = getattr(message, "subtype", None)
			data = getattr(message, "data", {}) or {}
			if subtype == "init":
				session.capabilities = data.get("capabilities", [])
				session.mcp_servers = data.get("mcp_servers", [])
				errors = data.get("mcp_server_errors", [])
				async with session.lock:
					if errors:
						session.transition(SessionState.ERROR, reason="mcp_server_errors")
						raise McpServerFailedError(errors)
					session.model = data.get("model", session.model)
					# init now arrives AFTER the first query (see session_create),
					# i.e. mid-turn while the session is RUNNING. Forcing IDLE here
					# unconditionally yanked the session out of its own turn and sent
					# the state machine into a retry loop that re-emitted init dozens
					# of times. Only the CREATING->IDLE promotion is ours to make.
					if session.state == SessionState.CREATING:
						session.transition(SessionState.IDLE, reason="init ok")
				self.persistence.write_meta(session.session_id, session.to_meta_dict())
			elif subtype == "api_retry":
				try:
					self.governor.handle_api_retry(data)
				except Exception:
					async with session.lock:
						session.transition(SessionState.ERROR, reason="auth failed")
				if self.governor.is_in_cooldown():
					async with session.lock:
						session.transition(SessionState.COOLDOWN, reason="rate_limit")

		elif msg_type == "ResultMessage":
			result_data = events[-1].data if events else {}
			turn = session.current_turn
			if turn:
				turn.end_cursor = session.event_log.cursor
				turn.state = "completed" if not getattr(message, "is_error", False) else "failed"
				turn.result_text = getattr(message, "result", None)
				turn.cost_usd = result_data.get("total_cost_usd")
				turn.usage = result_data.get("usage")
				turn.num_turns = result_data.get("num_turns")
				turn.stop_reason = result_data.get("stop_reason")
				session.turn_history.append(turn)
				self.ledger.update_session_cost(session.cost, result_data)
				self.ledger.record_turn(
					session.session_id,
					turn.turn_id,
					result_data,
				)
				self.permissions.deny_all_for_turn(session)
			self.governor.on_turn_complete()
			async with session.lock:
				session.current_turn = None
				if session.state == SessionState.COOLDOWN and not self.governor.is_in_cooldown():
					session.transition(SessionState.IDLE, reason="cooldown expired")
				elif session.state in (SessionState.RUNNING, SessionState.AWAITING_PERMISSION):
					session.transition(SessionState.IDLE, reason="turn complete")
			self.persistence.write_meta(session.session_id, session.to_meta_dict())

	def _build_options(
		self,
		session: Session,
		*,
		model: str | None = None,
		effort: str | None = None,
		system_prompt_append: str | None = None,
		allowed_tools: list[str] | None = None,
		disallowed_tools: list[str] | None = None,
		max_budget_usd: float | None = None,
		max_turns: int | None = None,
		mcp_servers: dict[str, Any] | None = None,
		agents: dict[str, Any] | None = None,
		additional_directories: list[str] | None = None,
		setting_sources: list[str] | None = None,
		resume_from: str | None = None,
		fork_from: str | None = None,
	) -> Any:
		from typing import cast

		from claude_agent_sdk import ClaudeAgentOptions

		policy = self.permissions.get_policy(session.permission_policy)
		store = FilesystemSessionStore(
			self.persistence.session_dir(session.session_id) / "store"
		)
		system_prompt: Any = None
		if system_prompt_append:
			system_prompt = {
				"type": "preset",
				"preset": "claude_code",
				"append": system_prompt_append,
			}
		return ClaudeAgentOptions(
			cwd=str(session.workspace),
			model=model,
			effort=cast(Any, effort),
			system_prompt=cast(Any, system_prompt),
			allowed_tools=allowed_tools or [],
			disallowed_tools=disallowed_tools or [],
			can_use_tool=self.permissions.make_callback(session.session_id, policy),
			permission_mode="default",
			max_budget_usd=max_budget_usd or session.max_budget_usd,
			max_turns=max_turns,
			mcp_servers=mcp_servers or {},
			strict_mcp_config=True,
			agents=agents,
			add_dirs=cast(Any, additional_directories or session.additional_directories),
			setting_sources=cast(Any, setting_sources or []),
			session_id=session.session_id,
			resume=resume_from or fork_from,
			fork_session=bool(fork_from),
			include_partial_messages=False,
			session_store=cast(Any, store),
			session_store_flush="batched",
			cli_path=str(self.config.cli_path),
			stderr=lambda line: logger.debug("cli", line=line),
			env={},
		)

	async def create_session(
		self,
		workspace: str,
		*,
		model: str | None = None,
		effort: str | None = None,
		system_prompt_append: str | None = None,
		permission_policy: str | None = None,
		allowed_tools: list[str] | None = None,
		disallowed_tools: list[str] | None = None,
		max_budget_usd: float | None = None,
		max_turns: int | None = None,
		mcp_servers: dict[str, Any] | None = None,
		agents: dict[str, Any] | None = None,
		additional_directories: list[str] | None = None,
		setting_sources: list[str] | None = None,
		resume_from: str | None = None,
		fork_from: str | None = None,
		label: str | None = None,
		idle_ttl_seconds: int | None = None,
		parent_session_id: str | None = None,
	) -> dict[str, Any]:
		self.governor.assert_sendable()

		if resume_from or fork_from:
			source_id = resume_from or fork_from
			meta = self.persistence.read_meta(source_id)  # type: ignore[arg-type]
			if meta is None and source_id not in self.registry._sessions:
				raise SessionNotFoundError(source_id)  # type: ignore[arg-type]

		ws = validate_workspace(workspace, self.config.workspace_roots)
		for ad in additional_directories or []:
			validate_workspace(ad, self.config.workspace_roots)

		async with self.registry._lock:
			if self.registry.count_live() >= self.config.max_sessions:
				raise SessionLimitReachedError(self.config.max_sessions)

			session_id = str(uuid.uuid4())
			session_dir = self.persistence.session_dir(session_id)
			event_log = EventLog(session_dir, memory_limit=self.config.event_memory_limit)
			session = Session(
				session_id=session_id,
				workspace=ws,
				event_log=event_log,
				model=model,
				permission_policy=permission_policy or self.config.default_policy,
				max_budget_usd=max_budget_usd or self.config.default_budget_usd,
				label=label,
				parent_session_id=parent_session_id or (fork_from if fork_from else None),
				idle_ttl_seconds=idle_ttl_seconds or self.config.session_idle_ttl,
				additional_directories=additional_directories,
			)
			session.options = self._build_options(
				session,
				model=model,
				effort=effort,
				system_prompt_append=system_prompt_append,
				allowed_tools=allowed_tools,
				disallowed_tools=disallowed_tools,
				max_budget_usd=max_budget_usd,
				max_turns=max_turns,
				mcp_servers=mcp_servers,
				agents=agents,
				additional_directories=additional_directories,
				setting_sources=setting_sources,
				resume_from=resume_from,
				fork_from=fork_from,
			)

			if self._client_factory:
				session.client = self._client_factory(session.options, session)
			else:
				from claude_agent_sdk import ClaudeSDKClient

				session.client = ClaudeSDKClient(options=session.options)
			await session.client.connect()

			self.registry.register(session)
			session.consumer_task = asyncio.create_task(self._consume(session))
			self.persistence.write_meta(session_id, session.to_meta_dict())

		# The real CLI does NOT emit SystemMessage(subtype="init") on connect() — it
		# emits it after the first query(). Verified against claude-agent-sdk 0.2.129
		# / CLI 2.1.219: 12s after connect yields no messages at all, and the first
		# query immediately yields init. Blocking indefinitely on init therefore hung
		# for 30s and failed EVERY real session with "Session init timed out". CI
		# never caught it because the test client factory emits init on connect.
		#
		# So: give init a short grace period (in-process/mocked clients deliver it in
		# milliseconds), then proceed as connected rather than failing.
		# _apply_side_effects still enriches model/capabilities/mcp_servers whenever
		# init does land. Consequence: mcp_server_errors surface on the first
		# session_send rather than at create time — which is where the work is.
		grace = float(os.environ.get("BROKER_INIT_GRACE_SECONDS", "2.0"))
		deadline = asyncio.get_event_loop().time() + grace
		while session.state == SessionState.CREATING:
			if asyncio.get_event_loop().time() > deadline:
				async with session.lock:
					if session.state == SessionState.CREATING:
						session.transition(SessionState.IDLE, reason="connected; init deferred")
				break
			await asyncio.sleep(0.05)

		if session.state == SessionState.ERROR:
			errors: list[Any] = []
			for ev in session.event_log._events:
				if ev.type == "session_init":
					errors = ev.data.get("mcp_server_errors", [])
			if errors:
				raise McpServerFailedError(errors)
			for ev in session.event_log._events:
				if ev.type == "error":
					raise InternalError(ev.data.get("message", "Session failed during init"))
			raise InternalError("Session failed during init")

		return {
			"session_id": session.session_id,
			"state": session.state.value,
			"workspace": str(session.workspace),
			"model": session.model,
			"permission_policy": session.permission_policy,
			"max_budget_usd": session.max_budget_usd,
			"cursor": session.event_log.cursor,
			"capabilities": session.capabilities,
			"mcp_servers": session.mcp_servers,
			"created_at": format_ts(session.created_at),
			"parent_session_id": session.parent_session_id,
		}

	def _assert_sendable(self, session: Session) -> None:
		if session.state in (SessionState.RUNNING, SessionState.AWAITING_PERMISSION):
			raise SessionBusyError(session.session_id)
		if session.state.is_terminal:
			raise SessionTerminalError(session.session_id, session.state.value)
		if session.state == SessionState.COOLDOWN:
			raise RateLimitedError(self.governor.retry_after_seconds())
		self.governor.assert_sendable()
		self.ledger.check_global_budget()

	async def send(
		self,
		session_id: str,
		prompt: str,
		wait_ms: int = 0,
	) -> dict[str, Any]:
		session = self.registry.get_or_raise(session_id)

		async with session.lock:
			self._assert_sendable(session)
			turn = Turn(
				turn_id=new_ulid(),
				session_id=session_id,
				prompt=prompt,
				started_at=utcnow(),
				start_cursor=session.event_log.cursor,
			)
			session.current_turn = turn
			session.transition(SessionState.RUNNING, reason="session_send")
			await session.client.query(prompt)

		if wait_ms > 0:
			await self._await_settled(session, turn, wait_ms)

		return self._send_snapshot(session, turn)

	async def _await_settled(self, session: Session, turn: Turn, wait_ms: int) -> None:
		deadline = asyncio.get_event_loop().time() + wait_ms / 1000
		while True:
			if turn.state != "running":
				return
			if session.state == SessionState.AWAITING_PERMISSION:
				return
			if self.permissions.list_pending(session.session_id):
				return
			if session.state in (SessionState.COOLDOWN, SessionState.ERROR):
				return
			if session.state == SessionState.IDLE and session.current_turn is None:
				return
			remaining = deadline - asyncio.get_event_loop().time()
			if remaining <= 0:
				return
			session.wakeup.clear()
			try:
				await asyncio.wait_for(session.wakeup.wait(), timeout=remaining)
			except TimeoutError:
				return

	def _send_snapshot(self, session: Session, turn: Turn) -> dict[str, Any]:
		events, _, _ = session.event_log.poll(cursor=turn.start_cursor)
		result = None
		if session.state == SessionState.IDLE and turn.state in (
			"completed",
			"failed",
			"interrupted",
		):
			for ev in reversed(events):
				if ev.get("type") == "turn_result":
					result = ev
					break
		return {
			"turn_id": turn.turn_id,
			"session_id": session.session_id,
			"state": session.state.value,
			"cursor": session.event_log.cursor,
			"events": events,
			"result": result,
			"pending_permissions": self.permissions.list_pending(session.session_id),
		}

	def poll(
		self,
		session_id: str,
		cursor: int = 0,
		wait_ms: int = 0,
		limit: int = 200,
		include: list[str] | None = None,
	) -> dict[str, Any]:
		session = self.registry.get_or_raise(session_id)

		if wait_ms > 0:
			events_before, _, _ = session.event_log.poll(cursor=cursor, limit=1)
			if not events_before:
				asyncio.get_event_loop()
				# sync path for MCP - use event loop if running
				try:
					loop = asyncio.get_running_loop()
					loop.create_task(self._poll_wait(session, cursor, wait_ms))
				except RuntimeError:
					pass

		return self._poll_snapshot(session, cursor, limit, include)

	async def _poll_wait(self, session: Session, cursor: int, wait_ms: int) -> None:
		deadline = asyncio.get_event_loop().time() + wait_ms / 1000
		while asyncio.get_event_loop().time() < deadline:
			events, _, _ = session.event_log.poll(cursor=cursor, limit=1)
			if events:
				session.wakeup.set()
				return
			session.wakeup.clear()
			remaining = deadline - asyncio.get_event_loop().time()
			if remaining <= 0:
				return
			try:
				await asyncio.wait_for(session.wakeup.wait(), timeout=remaining)
			except TimeoutError:
				return

	async def poll_async(
		self,
		session_id: str,
		cursor: int = 0,
		wait_ms: int = 0,
		limit: int = 200,
		include: list[str] | None = None,
	) -> dict[str, Any]:
		session = self.registry.get_or_raise(session_id)
		if wait_ms > 0:
			deadline = asyncio.get_event_loop().time() + wait_ms / 1000
			while asyncio.get_event_loop().time() < deadline:
				events, _, _ = session.event_log.poll(cursor=cursor, limit=1)
				if events:
					break
				if session.state == SessionState.AWAITING_PERMISSION:
					break
				session.wakeup.clear()
				remaining = deadline - asyncio.get_event_loop().time()
				if remaining <= 0:
					break
				try:
					await asyncio.wait_for(session.wakeup.wait(), timeout=remaining)
				except TimeoutError:
					break
		return self._poll_snapshot(session, cursor, limit, include)

	def _poll_snapshot(
		self,
		session: Session,
		cursor: int,
		limit: int,
		include: list[str] | None,
	) -> dict[str, Any]:
		events, next_cursor, has_more = session.event_log.poll(
			cursor=cursor, limit=limit, include=include
		)
		turn_cost = 0.0
		for ev in events:
			if ev.get("type") == "turn_result":
				turn_cost = ev.get("total_cost_usd", 0.0) or 0.0
		return {
			"session_id": session.session_id,
			"state": session.state.value,
			"cursor": next_cursor,
			"has_more": has_more,
			"events": events,
			"pending_permissions": self.permissions.list_pending(session.session_id),
			"cost": {
				"turn_usd": round_usd(float(turn_cost)),
				"session_usd": round_usd(session.cost.total_usd),
			},
		}

	async def interrupt(self, session_id: str) -> dict[str, Any]:
		session = self.registry.get_or_raise(session_id)
		interrupted_turn_id = None

		if session.state not in (SessionState.RUNNING, SessionState.AWAITING_PERMISSION):
			return {
				"session_id": session_id,
				"state": session.state.value,
				"interrupted_turn_id": None,
				"cursor": session.event_log.cursor,
			}

		if session.state == SessionState.AWAITING_PERMISSION:
			for req in self.permissions.list_pending(session_id):
				self.permissions.resolve(
					req["request_id"],
					"deny",
					reason="Interrupted",
					interrupt=True,
				)

		if session.current_turn:
			interrupted_turn_id = session.current_turn.turn_id

		await session.client.interrupt()

		deadline = asyncio.get_event_loop().time() + 10
		while session.state in (SessionState.RUNNING, SessionState.AWAITING_PERMISSION):
			if asyncio.get_event_loop().time() > deadline:
				async with session.lock:
					session.transition(SessionState.ERROR, reason="interrupt timeout")
				raise InterruptTimeoutError(session_id)
			await asyncio.sleep(0.05)

		if session.current_turn:
			session.current_turn.state = "interrupted"
			session.current_turn = None

		async with session.lock:
			if session.state not in (SessionState.ERROR, SessionState.CLOSED):
				session.transition(SessionState.IDLE, reason="interrupted")

		return {
			"session_id": session_id,
			"state": session.state.value,
			"interrupted_turn_id": interrupted_turn_id,
			"cursor": session.event_log.cursor,
		}

	async def close_session(self, session_id: str, reason: str = "") -> dict[str, Any]:
		session = self.registry.get(session_id)
		if session is None:
			meta = self.persistence.read_meta(session_id)
			if meta and meta.get("state") == "closed":
				return {
					"session_id": session_id,
					"state": "closed",
					"final_cost_usd": meta.get("final_cost_usd", 0),
					"turns": len(meta.get("turns", [])),
					"transcript_path": str(
						self.persistence.session_dir(session_id) / "events.jsonl"
					),
				}
			raise SessionNotFoundError(session_id)

		if session.state == SessionState.CLOSED:
			return {
				"session_id": session_id,
				"state": "closed",
				"final_cost_usd": round_usd(session.cost.total_usd),
				"turns": session.cost.turns,
				"transcript_path": str(session.session_dir / "events.jsonl"),
			}

		if session.consumer_task and not session.consumer_task.done():
			session.consumer_task.cancel()
			try:
				await session.consumer_task
			except asyncio.CancelledError:
				pass

		try:
			await session.client.disconnect()
		except Exception:
			pass

		async with session.lock:
			session.transition(SessionState.CLOSED, reason=reason or "session_close")

		session.event_log.flush()
		meta = session.to_meta_dict()
		meta["final_cost_usd"] = round_usd(session.cost.total_usd)
		self.persistence.write_meta(session_id, meta)
		self._closed_sessions[session_id] = meta

		return {
			"session_id": session_id,
			"state": "closed",
			"final_cost_usd": round_usd(session.cost.total_usd),
			"turns": session.cost.turns,
			"transcript_path": str(session.session_dir / "events.jsonl"),
		}

	async def fork_session(
		self,
		session_id: str,
		**overrides: Any,
	) -> dict[str, Any]:
		parent = self.registry.get_or_raise(session_id)
		if parent.state in (SessionState.RUNNING, SessionState.AWAITING_PERMISSION):
			raise SessionBusyError(session_id)
		return await self.create_session(
			str(overrides.get("workspace", parent.workspace)),
			model=overrides.get("model", parent.model),
			permission_policy=overrides.get("permission_policy", parent.permission_policy),
			max_budget_usd=overrides.get("max_budget_usd", parent.max_budget_usd),
			label=overrides.get("label"),
			fork_from=session_id,
			parent_session_id=session_id,
		)

	def list_sessions(
		self,
		state: list[str] | None = None,
		include_closed: bool = False,
		limit: int = 50,
	) -> dict[str, Any]:
		sessions = self.registry.list_all()
		result = []
		for s in sessions:
			if state and s.state.value not in state:
				continue
			if not include_closed and s.state == SessionState.CLOSED:
				continue
			pending = len(self.permissions.list_pending(s.session_id))
			result.append({
				"session_id": s.session_id,
				"label": s.label,
				"state": s.state.value,
				"workspace": str(s.workspace),
				"model": s.model,
				"cost_usd": round_usd(s.cost.total_usd),
				"budget_usd": s.max_budget_usd,
				"turns": s.cost.turns,
				"cursor": s.event_log.cursor,
				"pending_permissions": pending,
				"created_at": format_ts(s.created_at),
				"last_active_at": format_ts(s.last_active_at),
				"parent_session_id": s.parent_session_id,
			})
		return {
			"sessions": result[:limit],
			"total": len(result),
			"capacity": {"live": self.registry.count_live(), "max": self.config.max_sessions},
		}

	def transcript(
		self,
		session_id: str,
		format: str = "json",
		from_cursor: int = 0,
		to_cursor: int | None = None,
		include: list[str] | None = None,
	) -> dict[str, Any]:
		session = self.registry.get(session_id)
		if session:
			events, _, _ = session.event_log.poll(cursor=from_cursor, limit=100000, include=include)
		else:
			jsonl = self.persistence.session_dir(session_id) / "events.jsonl"
			all_events = EventLog.load_from_disk(jsonl)
			events = [
				e.to_dict() for e in all_events
				if e.index >= from_cursor and (to_cursor is None or e.index < to_cursor)
			]
			if include:
				events = [e for e in events if e.get("type") in include]

		if to_cursor is not None:
			events = [e for e in events if e.get("index", 0) < to_cursor]

		end_cursor = events[-1]["index"] + 1 if events else from_cursor
		content: Any = events
		if format == "markdown":
			lines = []
			for ev in events:
				lines.append(f"### [{ev.get('index')}] {ev.get('type')}")
				for k, v in ev.items():
					if k not in ("index", "type", "at"):
						lines.append(f"- **{k}**: {v}")
			content = "\n".join(lines)

		return {
			"session_id": session_id,
			"format": format,
			"from_cursor": from_cursor,
			"to_cursor": end_cursor,
			"content": content,
		}

	def broker_status(self) -> dict[str, Any]:
		from broker import __version__

		auth_probe = get_auth_probe_result() or {}
		token_info = decode_token_expiry(self.config.oauth_token)
		billing = self.config.billing_mode
		auth_billing = (
			"api" if billing == "api" else auth_probe.get("billing", "subscription")
		)

		health = "ok"
		warning = token_info.get("warning")
		if auth_billing == "api":
			health = "degraded"
			warning = "API billing active — charges are pay-as-you-go"

		try:
			cli_version = probe_cli_version(self.config.cli_path)
		except Exception:
			cli_version = "unknown"

		live = self.registry.count_live()
		running = sum(1 for s in self.registry.list_all() if s.state == SessionState.RUNNING)
		idle = sum(1 for s in self.registry.list_all() if s.state == SessionState.IDLE)

		budget_status = self.ledger.status()
		budget_status["billing_mode"] = auth_billing

		return {
			"version": __version__,
			"uptime_seconds": int((datetime.now(tz=UTC) - STARTUP_TIME).total_seconds()),
			"auth": {
				"method": auth_probe.get("method", "CLAUDE_CODE_OAUTH_TOKEN"),
				"billing": auth_billing,
				"token_expires_at": token_info.get("token_expires_at"),
				"days_remaining": token_info.get("days_remaining"),
				"warning": warning,
			},
			"cli": {"version": cli_version, "path": str(self.config.cli_path)},
			"sdk": {"version": "0.2.129"},
			"sessions": {
				"live": live,
				"idle": idle,
				"running": running,
				"max": self.config.max_sessions,
			},
			"budget": budget_status,
			"rate_limit": self.governor.status(),
			"workspace_roots": [str(r) for r in self.config.workspace_roots],
			"health": health,
		}
