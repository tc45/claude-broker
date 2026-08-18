"""Append-only event log with cursor addressing and JSONL persistence."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
	return datetime.now(tz=UTC)


def format_ts(dt: datetime) -> str:
	return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Event:
	index: int
	type: str
	at: str
	data: dict[str, Any] = field(default_factory=dict)

	def to_dict(self) -> dict[str, Any]:
		result = {"index": self.index, "type": self.type, "at": self.at}
		result.update(self.data)
		return result

	@classmethod
	def from_dict(cls, d: dict[str, Any]) -> Event:
		idx = d["index"]
		etype = d["type"]
		at = d["at"]
		data = {k: v for k, v in d.items() if k not in ("index", "type", "at")}
		return cls(index=idx, type=etype, at=at, data=data)

	@classmethod
	def permission_request(cls, idx: int, request: Any) -> Event:
		return cls(
			index=idx,
			type="permission_request",
			at=format_ts(request.requested_at),
			data={
				"request_id": request.request_id,
				"tool": request.tool,
				"input": request.input,
				"expires_at": format_ts(request.expires_at),
			},
		)

	@classmethod
	def permission_decision(cls, idx: int, request: Any) -> Event:
		decision = request.decision
		return cls(
			index=idx,
			type="permission_decision",
			at=format_ts(utcnow()),
			data={
				"request_id": request.request_id,
				"decision": decision.verdict if decision else "deny",
				"decided_by": getattr(decision, "decided_by", "timeout") if decision else "timeout",
				"reason": getattr(decision, "reason", None) if decision else None,
			},
		)

	@classmethod
	def state_change(cls, idx: int, from_state: str, to_state: str, reason: str) -> Event:
		return cls(
			index=idx,
			type="state_change",
			at=format_ts(utcnow()),
			data={"from": from_state, "to": to_state, "reason": reason},
		)


class EventLog:
	"""Cursor-addressed append-only event log with memory cap and JSONL sink."""

	def __init__(self, session_dir: Path, memory_limit: int = 5000) -> None:
		self._session_dir = session_dir
		self._memory_limit = memory_limit
		self._events: list[Event] = []
		self._cursor = 0
		self._lock = threading.Lock()
		self._jsonl_path = session_dir / "events.jsonl"
		self._session_dir.mkdir(parents=True, exist_ok=True)

	@property
	def cursor(self) -> int:
		return self._cursor

	def append(self, event: Event | list[Event]) -> None:
		"""Append one or more events, assigning monotonic indices."""
		events = event if isinstance(event, list) else [event]
		with self._lock:
			for ev in events:
				ev.index = self._cursor
				ev.at = ev.at or format_ts(utcnow())
				self._events.append(ev)
				self._write_jsonl(ev)
				self._cursor += 1
			self._trim_memory()

	def _write_jsonl(self, event: Event) -> None:
		with open(self._jsonl_path, "a", encoding="utf-8") as f:
			f.write(json.dumps(event.to_dict(), default=str) + "\n")

	def _trim_memory(self) -> None:
		if len(self._events) > self._memory_limit:
			excess = len(self._events) - self._memory_limit
			self._events = self._events[excess:]

	def poll(
		self,
		cursor: int = 0,
		limit: int = 200,
		include: list[str] | None = None,
	) -> tuple[list[dict[str, Any]], int, bool]:
		"""Return events from cursor, next cursor, and has_more flag."""
		all_events = self._get_events_from(cursor)
		if include:
			all_events = [e for e in all_events if e.type in include]
		truncated = len(all_events) > limit
		selected = all_events[:limit]
		next_cursor = cursor
		if selected:
			next_cursor = selected[-1].index + 1
		return [e.to_dict() for e in selected], next_cursor, truncated

	def _get_events_from(self, cursor: int) -> list[Event]:
		with self._lock:
			memory_start = self._events[0].index if self._events else self._cursor
			if cursor >= memory_start and self._events:
				return [e for e in self._events if e.index >= cursor]
		return self._read_from_disk(cursor)

	def _read_from_disk(self, cursor: int) -> list[Event]:
		if not self._jsonl_path.exists():
			return []
		events: list[Event] = []
		with open(self._jsonl_path, encoding="utf-8") as f:
			for line in f:
				line = line.strip()
				if not line:
					continue
				try:
					data = json.loads(line)
					ev = Event.from_dict(data)
					if ev.index >= cursor:
						events.append(ev)
				except (json.JSONDecodeError, KeyError):
					continue
		return events

	@staticmethod
	def load_from_disk(jsonl_path: Path) -> list[Event]:
		"""Load events from JSONL, tolerating corrupt trailing line."""
		events: list[Event] = []
		if not jsonl_path.exists():
			return events
		with open(jsonl_path, encoding="utf-8") as f:
			for line in f:
				line = line.strip()
				if not line:
					continue
				try:
					data = json.loads(line)
					events.append(Event.from_dict(data))
				except (json.JSONDecodeError, KeyError):
					break
		return events

	def flush(self) -> None:
		"""Ensure all events are on disk (already append-only)."""
		pass
