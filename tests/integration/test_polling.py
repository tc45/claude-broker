"""Integration tests for polling and wait_ms semantics."""

from __future__ import annotations

import asyncio
import time

import pytest

from broker.config import Config
from broker.core import BrokerCore
from tests.conftest import (
	AssistantMessage,
	FakeSDKClient,
	ResultMessage,
	TextBlock,
	ToolUseRequest,
	init_script,
)


@pytest.fixture
def slow_script() -> list:
	return init_script() + [
		(0.5, AssistantMessage(content=[TextBlock(text="slow")])),
		ResultMessage(total_cost_usd=0.01),
	]


@pytest.mark.integration
async def test_i11_wait_zero_immediate(test_config: Config, tmp_workspace, slow_script) -> None:
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(slow_script))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	start = time.monotonic()
	result = await core.send(s["session_id"], "go", wait_ms=0)
	elapsed = time.monotonic() - start
	assert elapsed < 0.3
	assert result["state"] == "running"


@pytest.mark.integration
async def test_i12_wait_settles_early(test_config: Config, tmp_workspace, slow_script) -> None:
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(slow_script))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	start = time.monotonic()
	result = await core.send(s["session_id"], "go", wait_ms=5000)
	elapsed = time.monotonic() - start
	assert elapsed < 3.0
	assert result["state"] == "idle"


@pytest.mark.integration
async def test_i13_wait_timeout(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [("block", True), ResultMessage()]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	# Release after send starts
	async def release_later() -> None:
		await asyncio.sleep(0.2)
		for sess in core.registry.list_all():
			if hasattr(sess.client, "release"):
				sess.client.release()

	asyncio.create_task(release_later())
	start = time.monotonic()
	result = await core.send(s["session_id"], "go", wait_ms=1000)
	elapsed = time.monotonic() - start
	assert elapsed < 2.0


@pytest.mark.integration
async def test_i14_permission_early_return(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [ToolUseRequest(tool="Bash", input={"command": "curl http://example.com"})]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	start = time.monotonic()
	result = await core.send(s["session_id"], "run tests", wait_ms=30000)
	elapsed = time.monotonic() - start
	assert elapsed < 5.0
	assert result["state"] == "awaiting_permission" or result["pending_permissions"]


@pytest.mark.integration
async def test_i15_idempotent_poll(test_config: Config, tmp_workspace) -> None:
	from tests.conftest import complete_turn_script

	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	await core.send(s["session_id"], "go", wait_ms=5000)
	p1 = await core.poll_async(s["session_id"], cursor=0)
	p2 = await core.poll_async(s["session_id"], cursor=0)
	assert p1["events"] == p2["events"]


@pytest.mark.integration
async def test_i16_poll_after_restart(test_config: Config, tmp_workspace) -> None:
	from tests.conftest import complete_turn_script

	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	await core.send(s["session_id"], "go", wait_ms=5000)
	sid = s["session_id"]
	session = core.registry.get(sid)
	events_before = session.event_log.poll(cursor=0)[0]
	await core.close_session(sid)
	core2 = BrokerCore(test_config)
	transcript = core2.transcript(sid)
	assert len(transcript["content"]) >= len(events_before)
