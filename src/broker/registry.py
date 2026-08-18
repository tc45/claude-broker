"""Session registry, state machine, and turn model."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from broker.errors import SessionNotFoundError
from broker.event_log import Event, EventLog, format_ts


def utcnow() -> datetime:
	return datetime.now(tz=UTC)


def new_ulid() -> str:
	"""Generate a sortable turn ID."""
	import time

	ts = int(time.time() * 1000)
	rand = uuid.uuid4().hex[:16]
	return f"t_{ts:013x}{rand}"


class SessionState(StrEnum):
	CREATING = "creating"
	IDLE = "idle"
	RUNNING = "running"
	AWAITING_PERMISSION = "awaiting_permission"
	COOLDOWN = "cooldown"
	ERROR = "error"
	CLOSED = "closed"

	@property
	def is_terminal(self) -> bool:
		return self in (SessionState.ERROR, SessionState.CLOSED)

	@property
	def accepts_send(self) -> bool:
		return self == SessionState.IDLE


@dataclass
class Turn:
	turn_id: str
	session_id: str
	prompt: str
	started_at: datetime
	start_cursor: int
	end_cursor: int | None = None
	state: Literal["running", "completed", "failed", "interrupted"] = "running"
	result_text: str | None = None
	cost_usd: float | None = None
	usage: dict[str, Any] | None = None
	num_turns: int | None = None
	stop_reason: str | None = None
	permission_requests: list[str] = field(default_factory=list)


@dataclass
class SessionMeta:
	session_id: str
	workspace: str
	model: str | None
	permission_policy: str
	max_budget_usd: float
	label: str | None
	parent_session_id: str | None
	idle_ttl_seconds: int
	created_at: str
	last_active_at: str
	state: str
	turns: list[dict[str, Any]] = field(default_factory=list)


class Session:
	"""A live broker session wrapping a ClaudeSDKClient."""

	def __init__(
		self,
		session_id: str,
		workspace: Path,
		event_log: EventLog,
		*,
		model: str | None = None,
		permission_policy: str = "reviewed",
		max_budget_usd: float = 2.0,
		label: str | None = None,
		parent_session_id: str | None = None,
		idle_ttl_seconds: int = 3600,
		additional_directories: list[str] | None = None,
	) -> None:
		self.session_id = session_id
		self.workspace = workspace
		self.event_log = event_log
		self.model = model
		self.permission_policy = permission_policy
		self.max_budget_usd = max_budget_usd
		self.label = label
		self.parent_session_id = parent_session_id
		self.idle_ttl_seconds = idle_ttl_seconds
		self.additional_directories = additional_directories or []
		self.state = SessionState.CREATING
		self.client: Any = None
		self.options: Any = None
		self.current_turn: Turn | None = None
		self.cost = SessionCost()
		self.created_at = utcnow()
		self.last_active_at = utcnow()
		self.lock = asyncio.Lock()
		self.wakeup = asyncio.Event()
		self.consumer_task: asyncio.Task[None] | None = None
		self.capabilities: list[str] = []
		self.mcp_servers: list[Any] = []
		self.turn_history: list[Turn] = []
		self.session_dir = event_log._session_dir

	def transition(self, new_state: SessionState, reason: str = "") -> None:
		old = self.state
		if old == new_state:
			return
		self.state = new_state
		self.last_active_at = utcnow()
		self.event_log.append(
			Event.state_change(self.event_log.cursor, old.value, new_state.value, reason)
		)
		self._assert_invariants()

	def _assert_invariants(self) -> None:
		running_states = {SessionState.RUNNING, SessionState.AWAITING_PERMISSION}
		has_active_turn = self.current_turn is not None and self.current_turn.state == "running"
		if self.state in running_states:
			assert has_active_turn, "running state requires active turn"
		else:
			if self.state not in (SessionState.CREATING,):
				assert not has_active_turn or self.current_turn is None, (
					"non-running state should not have running turn"
				)
		if self.state in (SessionState.CLOSED, SessionState.ERROR):
			assert self.consumer_task is None or self.consumer_task.done(), (
				"terminal state should not have live consumer"
			)

	def to_meta_dict(self) -> dict[str, Any]:
		return {
			"session_id": self.session_id,
			"workspace": str(self.workspace),
			"model": self.model,
			"permission_policy": self.permission_policy,
			"max_budget_usd": self.max_budget_usd,
			"label": self.label,
			"parent_session_id": self.parent_session_id,
			"idle_ttl_seconds": self.idle_ttl_seconds,
			"created_at": format_ts(self.created_at),
			"last_active_at": format_ts(self.last_active_at),
			"state": self.state.value,
			"turns": [
				{
					"turn_id": t.turn_id,
					"state": t.state,
					"cost_usd": t.cost_usd,
				}
				for t in self.turn_history
			],
		}


@dataclass
class SessionCost:
	total_usd: float = 0.0
	turns: int = 0
	input_tokens: int = 0
	output_tokens: int = 0
	cache_read_tokens: int = 0
	cache_creation_tokens: int = 0
	by_model: dict[str, float] = field(default_factory=dict)


class SessionRegistry:
	"""Owns all live sessions with registry-level locking."""

	def __init__(self, max_sessions: int) -> None:
		self._sessions: dict[str, Session] = {}
		self._max_sessions = max_sessions
		self._lock = asyncio.Lock()

	@property
	def max_sessions(self) -> int:
		return self._max_sessions

	def count_live(self) -> int:
		return sum(
			1 for s in self._sessions.values()
			if s.state not in (SessionState.CLOSED, SessionState.ERROR)
		)

	def get(self, session_id: str) -> Session | None:
		return self._sessions.get(session_id)

	def get_or_raise(self, session_id: str) -> Session:
		session = self.get(session_id)
		if session is None:
			raise SessionNotFoundError(session_id)
		return session

	def register(self, session: Session) -> None:
		self._sessions[session.session_id] = session

	def remove(self, session_id: str) -> None:
		self._sessions.pop(session_id, None)

	def list_all(self) -> list[Session]:
		return list(self._sessions.values())

	async def acquire_registry_lock(self) -> asyncio.Lock:
		await self._lock.acquire()
		return self._lock

	def release_registry_lock(self) -> None:
		self._lock.release()
