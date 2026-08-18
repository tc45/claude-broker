"""Unit tests for event log."""

from __future__ import annotations

from pathlib import Path

from broker.event_log import Event, EventLog
from broker.normalise import normalise


def test_u30_cursor_monotonic(tmp_path: Path) -> None:
	log = EventLog(tmp_path / "s1", memory_limit=100)
	for i in range(10):
		log.append(Event(0, "test", "", data={"n": i}))
	assert log.cursor == 10
	indices = [e.index for e in log._events]
	assert indices == list(range(0, 10))


def test_u31_poll_from_cursor(tmp_path: Path) -> None:
	log = EventLog(tmp_path / "s1")
	for i in range(5):
		log.append(Event(0, "test", "", data={"n": i}))
	events, next_c, _ = log.poll(cursor=2)
	assert all(e["index"] >= 2 for e in events)
	assert events[0]["index"] == 2


def test_u32_poll_beyond_end(tmp_path: Path) -> None:
	log = EventLog(tmp_path / "s1")
	log.append(Event(0, "test", "", data={}))
	events, next_c, _ = log.poll(cursor=5)
	assert events == []
	assert next_c == 5


def test_u33_disk_readthrough(tmp_path: Path) -> None:
	log = EventLog(tmp_path / "s1", memory_limit=5)
	for i in range(20):
		log.append(Event(0, "test", "", data={"n": i}))
	mem_events, _, _ = log.poll(cursor=0, limit=100)
	disk_log = EventLog(tmp_path / "s1", memory_limit=5)
	disk_events, _, _ = disk_log.poll(cursor=0, limit=100)
	assert len(mem_events) == len(disk_events)
	for a, b in zip(mem_events, disk_events, strict=True):
		assert a == b


def test_u34_limit_truncation(tmp_path: Path) -> None:
	log = EventLog(tmp_path / "s1")
	for i in range(10):
		log.append(Event(0, "test", "", data={"n": i}))
	events, next_c, has_more = log.poll(cursor=0, limit=3)
	assert len(events) == 3
	assert has_more is True
	assert next_c == 3


def test_u35_jsonl_order(tmp_path: Path) -> None:
	log = EventLog(tmp_path / "s1")
	for i in range(5):
		log.append(Event(0, "test", "", data={"n": i}))
	reloaded = EventLog.load_from_disk(log._jsonl_path)
	assert len(reloaded) == 5
	for i, ev in enumerate(reloaded):
		assert ev.data["n"] == i


def test_u36_unmapped_message(tmp_path: Path) -> None:
	class UnknownMsg:
		pass

	events = normalise(UnknownMsg(), 0)
	assert len(events) == 1
	assert events[0].type == "error"
	assert events[0].data["code"] == "UNMAPPED_MESSAGE"
