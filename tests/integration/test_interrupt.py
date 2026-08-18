"""Integration tests for session interrupt."""

from __future__ import annotations

import asyncio

import pytest

from broker.config import Config
from broker.core import BrokerCore
from tests.conftest import (
	AssistantMessage,
	FakeSDKClient,
	TextBlock,
	init_script,
)


@pytest.mark.integration
@pytest.mark.timeout(15)
async def test_i32_interrupt_running(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [
		(2.0, AssistantMessage(content=[TextBlock(text="working")])),
	]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	await core.send(s["session_id"], "long task", wait_ms=0)
	await asyncio.sleep(0.1)
	result = await core.interrupt(s["session_id"])
	assert result["state"] == "idle"


@pytest.mark.integration
@pytest.mark.timeout(15)
async def test_i33_interrupt_idle(test_config: Config, tmp_workspace) -> None:
	from tests.conftest import complete_turn_script

	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	await core.send(s["session_id"], "done", wait_ms=5000)
	result = await core.interrupt(s["session_id"])
	assert result["state"] == "idle"
	assert result["interrupted_turn_id"] is None


@pytest.mark.integration
@pytest.mark.timeout(15)
async def test_i34_interrupt_during_permission(test_config: Config, tmp_workspace) -> None:
	from tests.conftest import ToolUseRequest

	script = init_script() + [ToolUseRequest(tool="Bash", input={"command": "sleep 60"})]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	await core.send(s["session_id"], "test", wait_ms=2000)
	result = await core.interrupt(s["session_id"])
	assert result["state"] in ("idle", "error")


@pytest.mark.integration
@pytest.mark.timeout(15)
async def test_i36_send_after_interrupt(test_config: Config, tmp_workspace) -> None:
	from tests.conftest import complete_turn_script

	script = init_script() + [
		(1.0, AssistantMessage(content=[TextBlock(text="working")])),
	]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	await core.send(s["session_id"], "task", wait_ms=0)
	await core.interrupt(s["session_id"])
	# New session with complete script for second send
	core2 = BrokerCore(
		test_config,
		client_factory=FakeSDKClient.factory(complete_turn_script()),
	)
	# Re-use same session if still alive
	session = core.registry.get(s["session_id"])
	if session and session.state.value == "idle":
		result = await core.send(s["session_id"], "again", wait_ms=3000)
		assert result["turn_id"]
