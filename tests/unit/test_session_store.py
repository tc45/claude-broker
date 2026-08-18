"""Unit tests for session store."""

from __future__ import annotations

from pathlib import Path

import pytest

from broker.session_store import FilesystemSessionStore, SessionPersistence


KEY = {"session_id": "abc-123"}


def test_store_implements_what_the_sdk_actually_calls() -> None:
	"""The regression that let the bug ship.

	The old test exercised get/set/delete/list_keys and passed, while the SDK
	called append/load and raised AttributeError on every turn. Assert the
	protocol's required surface, not an invented one.
	"""
	for name in ("append", "load"):
		assert callable(getattr(FilesystemSessionStore, name, None)), (
			f"SessionStore protocol requires {name}()"
		)


@pytest.mark.asyncio
async def test_append_then_load_roundtrips(tmp_path: Path) -> None:
	store = FilesystemSessionStore(tmp_path / "store")
	assert await store.load(KEY) is None  # nothing stored yet

	await store.append(KEY, [{"type": "user", "uuid": "u1"}])
	await store.append(KEY, [{"type": "assistant", "uuid": "u2"}])

	entries = await store.load(KEY)
	assert [e["uuid"] for e in entries] == ["u1", "u2"]


@pytest.mark.asyncio
async def test_append_dedupes_on_uuid(tmp_path: Path) -> None:
	"""A batch that times out is retried, so the same uuid can arrive twice."""
	store = FilesystemSessionStore(tmp_path / "store")
	batch = [{"type": "user", "uuid": "u1"}, {"type": "assistant", "uuid": "u2"}]
	await store.append(KEY, batch)
	await store.append(KEY, batch)
	assert len(await store.load(KEY)) == 2


@pytest.mark.asyncio
async def test_entries_without_uuid_are_kept(tmp_path: Path) -> None:
	"""Titles, tags and mode markers have no uuid and must not be deduped away."""
	store = FilesystemSessionStore(tmp_path / "store")
	await store.append(KEY, [{"type": "title"}, {"type": "title"}])
	assert len(await store.load(KEY)) == 2


@pytest.mark.asyncio
async def test_subpath_is_a_separate_transcript(tmp_path: Path) -> None:
	store = FilesystemSessionStore(tmp_path / "store")
	sub = {"session_id": "abc-123", "subpath": "agent-1"}
	await store.append(KEY, [{"type": "user", "uuid": "u1"}])
	await store.append(sub, [{"type": "user", "uuid": "s1"}])

	assert [e["uuid"] for e in await store.load(KEY)] == ["u1"]
	assert [e["uuid"] for e in await store.load(sub)] == ["s1"]
	# A subagent transcript is not a session in its own right.
	assert [s["session_id"] for s in await store.list_sessions()] == ["abc-123"]


@pytest.mark.asyncio
async def test_a_torn_final_line_does_not_fail_the_resume(tmp_path: Path) -> None:
	store = FilesystemSessionStore(tmp_path / "store")
	await store.append(KEY, [{"type": "user", "uuid": "u1"}])
	path = tmp_path / "store" / "abc-123.jsonl"
	with path.open("a", encoding="utf-8") as fh:
		fh.write('{"type": "assist')  # process killed mid-write

	entries = await store.load(KEY)
	assert [e["uuid"] for e in entries] == ["u1"]


@pytest.mark.asyncio
async def test_subpath_cannot_escape_the_store_dir(tmp_path: Path) -> None:
	store = FilesystemSessionStore(tmp_path / "store")
	await store.append(
		{"session_id": "abc", "subpath": "../../escaped"},
		[{"type": "user", "uuid": "u1"}],
	)
	assert not (tmp_path / "escaped.jsonl").exists()
	assert list((tmp_path / "store").glob("*.jsonl"))


@pytest.mark.asyncio
async def test_delete_removes_the_transcript(tmp_path: Path) -> None:
	store = FilesystemSessionStore(tmp_path / "store")
	await store.append(KEY, [{"type": "user", "uuid": "u1"}])
	await store.delete(KEY)
	assert await store.load(KEY) is None


def test_persistence_recovery(tmp_path: Path) -> None:
	p = SessionPersistence(tmp_path)
	sid = "abc-123"
	p.write_meta(sid, {"state": "running", "session_id": sid})
	recovered = p.recover_on_startup()
	assert sid in recovered
	meta = p.read_meta(sid)
	assert meta["state"] == "error"
