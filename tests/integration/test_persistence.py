"""Integration tests for persistence."""

from __future__ import annotations

import json

import pytest

from broker.config import Config
from broker.core import BrokerCore
from broker.event_log import EventLog
from tests.conftest import FakeSDKClient, complete_turn_script


@pytest.mark.integration
async def test_i42_restart_recovery(test_config: Config, tmp_workspace) -> None:
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	sid = s["session_id"]
	# Simulate running state on disk
	meta_path = test_config.state_dir / "sessions" / sid / "meta.json"
	meta = json.loads(meta_path.read_text())
	meta["state"] = "running"
	meta_path.write_text(json.dumps(meta))
	core2 = BrokerCore(test_config)
	recovered = core2.persistence.read_meta(sid)
	assert recovered["state"] == "error"
	assert recovered.get("reason") == "broker restarted"


@pytest.mark.integration
async def test_i43_transcript_after_restart(test_config: Config, tmp_workspace) -> None:
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	await core.send(s["session_id"], "hello", wait_ms=5000)
	sid = s["session_id"]
	await core.close_session(sid)
	core2 = BrokerCore(test_config)
	t = core2.transcript(sid)
	assert len(t["content"]) > 0


@pytest.mark.integration
async def test_i44_ledger_survives(test_config: Config, tmp_workspace) -> None:
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	await core.send(s["session_id"], "hello", wait_ms=5000)
	spent = core.ledger._global_spent
	core2 = BrokerCore(test_config)
	assert core2.ledger._global_spent == spent


@pytest.mark.integration
async def test_i45_transcript_closed(test_config: Config, tmp_workspace) -> None:
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	await core.send(s["session_id"], "hello", wait_ms=5000)
	await core.close_session(s["session_id"])
	core.registry.remove(s["session_id"])
	t = core.transcript(s["session_id"])
	assert len(t["content"]) > 0


@pytest.mark.integration
def test_i46_corrupt_jsonl_tail(test_config: Config) -> None:
	import tempfile
	from pathlib import Path

	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "events.jsonl"
		p.write_text('{"index": 0, "type": "test", "at": "2026-01-01T00:00:00Z"}\n{bad json\n')
		events = EventLog.load_from_disk(p)
		assert len(events) == 1
