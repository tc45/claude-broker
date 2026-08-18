"""Filesystem session store implementing the SDK SessionStore protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class FilesystemSessionStore:
	"""SDK-compatible filesystem session store."""

	def __init__(self, store_dir: Path) -> None:
		self._store_dir = store_dir
		self._store_dir.mkdir(parents=True, exist_ok=True)
		self._data_path = self._store_dir / "session.json"

	async def get(self, key: str) -> bytes | None:
		path = self._store_dir / f"{key}.bin"
		if path.exists():
			return path.read_bytes()
		if self._data_path.exists():
			data = json.loads(self._data_path.read_text(encoding="utf-8"))
			val = data.get(key)
			if val is not None:
				return val.encode() if isinstance(val, str) else bytes(val)
		return None

	async def set(self, key: str, value: bytes) -> None:
		path = self._store_dir / f"{key}.bin"
		path.write_bytes(value)

	async def delete(self, key: str) -> None:
		path = self._store_dir / f"{key}.bin"
		if path.exists():
			path.unlink()

	async def list_keys(self) -> list[str]:
		return [p.stem for p in self._store_dir.glob("*.bin")]


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
