"""Filesystem session store implementing the SDK SessionStore protocol.

The protocol is `append` + `load`, both required; the rest is optional and
probed for at runtime (the SDK duck-types — it never calls isinstance).

This file previously exposed `get`/`set`/`delete`/`list_keys`, a generic blob
store matching nothing the SDK calls. The mismatch was invisible to the tests
and loud in production: every turn raised
`'FilesystemSessionStore' object has no attribute 'append'`, the SDK retried
three times and surfaced a MirrorErrorMessage, and nothing was ever mirrored —
so a resume would have loaded an empty transcript from a store that looked
present and correctly configured.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# SessionKey is {"session_id": str, "subpath": NotRequired[str]}. `subpath`
# names a subagent transcript and is explicitly opaque — a key suffix, nothing
# to parse. Empty string is invalid per the protocol, so treat it as absent.
KeyDict = dict[str, Any]


def _key_parts(key: KeyDict | str) -> tuple[str, str]:
	if isinstance(key, str):  # tolerated: a bare id, should the SDK pass one
		return key, ""
	return str(key.get("session_id", "")), str(key.get("subpath") or "")


def _safe(name: str) -> str:
	"""A filesystem-safe fragment.

	Keys come from the SDK rather than a user, but `subpath` is documented as
	opaque, and an opaque string containing a separator would write outside the
	store directory.
	"""
	return "".join(c if c.isalnum() or c in "-_." else "-" for c in name) or "_"


class FilesystemSessionStore:
	"""Mirrors transcripts to JSONL, one file per session key."""

	def __init__(self, store_dir: Path) -> None:
		self._store_dir = store_dir
		self._store_dir.mkdir(parents=True, exist_ok=True)

	def _path(self, key: KeyDict | str) -> Path:
		session_id, subpath = _key_parts(key)
		name = _safe(session_id)
		if subpath:
			name = f"{name}__{_safe(subpath)}"
		return self._store_dir / f"{name}.jsonl"

	async def append(self, key: KeyDict | str, entries: list[dict[str, Any]]) -> None:
		"""Mirror a batch of transcript entries.

		Most entries carry a stable `uuid` that the protocol says to treat as an
		idempotency key. That matters here rather than being a nicety: a batch
		that times out is retried, so without the dedup a retried batch lands
		twice. Entries with no `uuid` (titles, tags, mode markers) are appended
		as-is, which is what the protocol asks for.
		"""
		if not entries:
			return
		path = self._path(key)
		seen = self._uuids(path)
		fresh = []
		for e in entries:
			uid = e.get("uuid") if isinstance(e, dict) else None
			if uid is not None:
				if uid in seen:
					continue
				seen.add(uid)
			fresh.append(e)
		if not fresh:
			return
		with path.open("a", encoding="utf-8") as fh:
			for e in fresh:
				fh.write(json.dumps(e, ensure_ascii=False) + "\n")

	async def load(self, key: KeyDict | str) -> list[dict[str, Any]] | None:
		"""Read a session back for resume. None means "nothing stored here"."""
		path = self._path(key)
		if not path.exists():
			return None
		out: list[dict[str, Any]] = []
		with path.open("r", encoding="utf-8") as fh:
			for line in fh:
				line = line.strip()
				if not line:
					continue
				try:
					out.append(json.loads(line))
				except ValueError:
					# A torn last line from a killed process. Dropping one
					# truncated record beats failing the whole resume.
					continue
		return out

	async def delete(self, key: KeyDict | str) -> None:
		"""Optional in the protocol; the SDK never deletes on its own."""
		path = self._path(key)
		if path.exists():
			path.unlink()

	async def list_sessions(self) -> list[dict[str, Any]]:
		"""Optional. `mtime` is epoch **milliseconds**, per the protocol."""
		out = []
		for p in sorted(self._store_dir.glob("*.jsonl")):
			if "__" in p.stem:  # a subagent transcript, not a session of its own
				continue
			out.append({"session_id": p.stem, "mtime": int(p.stat().st_mtime * 1000)})
		return out

	def _uuids(self, path: Path) -> set[str]:
		if not path.exists():
			return set()
		seen: set[str] = set()
		with path.open("r", encoding="utf-8") as fh:
			for line in fh:
				line = line.strip()
				if not line:
					continue
				try:
					rec = json.loads(line)
				except ValueError:
					continue
				uid = rec.get("uuid") if isinstance(rec, dict) else None
				if uid is not None:
					seen.add(uid)
		return seen


class SessionPersistence:
	"""Manages meta.json and session recovery on restart."""

	def __init__(self, state_dir: Path) -> None:
		self._state_dir = state_dir
		self._sessions_dir = state_dir / "sessions"

	def session_dir(self, session_id: str) -> Path:
		path = self._sessions_dir / session_id
		path.mkdir(parents=True, exist_ok=True)
		return path

	def write_meta(self, session_id: str, meta: dict[str, Any]) -> None:
		path = self.session_dir(session_id) / "meta.json"
		path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

	def read_meta(self, session_id: str) -> dict[str, Any] | None:
		path = self.session_dir(session_id) / "meta.json"
		if not path.exists():
			return None
		return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]

	def recover_on_startup(self) -> list[str]:
		"""Mark non-terminal sessions as error after broker restart."""
		recovered: list[str] = []
		if not self._sessions_dir.exists():
			return recovered
		terminal = {"closed", "error"}
		for session_path in self._sessions_dir.iterdir():
			if not session_path.is_dir():
				continue
			meta_path = session_path / "meta.json"
			if not meta_path.exists():
				continue
			meta = json.loads(meta_path.read_text(encoding="utf-8"))
			if meta.get("state") not in terminal:
				meta["state"] = "error"
				meta["reason"] = "broker restarted"
				meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
				recovered.append(session_path.name)
		return recovered

	def list_session_ids(self) -> list[str]:
		if not self._sessions_dir.exists():
			return []
		return [p.name for p in self._sessions_dir.iterdir() if p.is_dir()]
