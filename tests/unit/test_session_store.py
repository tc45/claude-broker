"""Unit tests for session store."""

from __future__ import annotations

from pathlib import Path

import pytest

from broker.session_store import FilesystemSessionStore, SessionPersistence


@pytest.mark.asyncio
async def test_session_store_roundtrip(tmp_path: Path) -> None:
	store = FilesystemSessionStore(tmp_path / "store")
	await store.set("key1", b"value1")
	assert await store.get("key1") == b"value1"
	keys = await store.list_keys()
	assert "key1" in keys
	await store.delete("key1")
	assert await store.get("key1") is None


def test_persistence_recovery(tmp_path: Path) -> None:
	p = SessionPersistence(tmp_path)
	sid = "abc-123"
	p.write_meta(sid, {"state": "running", "session_id": sid})
	recovered = p.recover_on_startup()
	assert sid in recovered
	meta = p.read_meta(sid)
	assert meta["state"] == "error"
