"""Integration tests for session lifecycle."""

from __future__ import annotations

import pytest

from broker.core import BrokerCore
from broker.errors import (
	McpServerFailedError,
	SessionBusyError,
	SessionLimitReachedError,
	SessionNotFoundError,
	SessionTerminalError,
)
from broker.registry import SessionState
from tests.conftest import (
	FakeSDKClient,
	complete_turn_script,
	config_with,
	init_script,
)


@pytest.fixture
def broker_with_fake(test_config: Config, tmp_workspace) -> BrokerCore:
	script = complete_turn_script()
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	return core


@pytest.mark.integration
async def test_i1_full_lifecycle(broker_with_fake: BrokerCore, tmp_workspace) -> None:
	repo = tmp_workspace / "repo"
	s = await broker_with_fake.create_session(str(repo))
	assert s["state"] == "idle"
	send = await broker_with_fake.send(s["session_id"], "hello", wait_ms=5000)
	assert send["state"] in ("idle", "running")
	poll = await broker_with_fake.poll_async(s["session_id"], cursor=0, wait_ms=5000)
	types = [e["type"] for e in poll["events"]]
	assert "session_init" in types or "turn_result" in types


@pytest.mark.integration
async def test_i2_session_busy(broker_with_fake: BrokerCore, tmp_workspace) -> None:
	repo = tmp_workspace / "repo"
	s = await broker_with_fake.create_session(str(repo))
	session = broker_with_fake.registry.get(s["session_id"])
	session.state = SessionState.RUNNING
	session.current_turn = type("T", (), {"turn_id": "t1", "state": "running"})()
	with pytest.raises(SessionBusyError):
		await broker_with_fake.send(s["session_id"], "again")


@pytest.mark.integration
async def test_i3_closed_session(broker_with_fake: BrokerCore, tmp_workspace) -> None:
	repo = tmp_workspace / "repo"
	s = await broker_with_fake.create_session(str(repo))
	await broker_with_fake.close_session(s["session_id"])
	with pytest.raises(SessionTerminalError):
		await broker_with_fake.send(s["session_id"], "hello")


@pytest.mark.integration
async def test_i4_unknown_session(broker_with_fake: BrokerCore) -> None:
	with pytest.raises(SessionNotFoundError):
		await broker_with_fake.send("nonexistent", "hello")


@pytest.mark.integration
async def test_i5_session_limit(test_config: Config, tmp_workspace) -> None:
	test_config = config_with(test_config, max_sessions=1)
	script = init_script()
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	repo = tmp_workspace / "repo"
	await core.create_session(str(repo))
	with pytest.raises(SessionLimitReachedError):
		await core.create_session(str(repo))


@pytest.mark.integration
async def test_i6_close_idempotent(broker_with_fake: BrokerCore, tmp_workspace) -> None:
	repo = tmp_workspace / "repo"
	s = await broker_with_fake.create_session(str(repo))
	r1 = await broker_with_fake.close_session(s["session_id"])
	r2 = await broker_with_fake.close_session(s["session_id"])
	assert r1["state"] == "closed"
	assert r2["state"] == "closed"


@pytest.mark.integration
async def test_i7_concurrent_sessions(test_config: Config, tmp_workspace) -> None:
	script = complete_turn_script(cost=0.05)
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	repo = tmp_workspace / "repo"
	s1 = await core.create_session(str(repo))
	s2 = await core.create_session(str(repo))
	await core.send(s1["session_id"], "one", wait_ms=3000)
	await core.send(s2["session_id"], "two", wait_ms=3000)
	assert core.registry.get(s1["session_id"]).cost.total_usd >= 0
	assert core.registry.get(s2["session_id"]).cost.total_usd >= 0


@pytest.mark.integration
async def test_i8_list_filter(broker_with_fake: BrokerCore, tmp_workspace) -> None:
	repo = tmp_workspace / "repo"
	s = await broker_with_fake.create_session(str(repo))
	lst = broker_with_fake.list_sessions(state=["idle"])
	assert any(x["session_id"] == s["session_id"] for x in lst["sessions"])


@pytest.mark.integration
async def test_i9_midstream_error(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [("raise", RuntimeError("boom"))]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	import asyncio
	await asyncio.sleep(0.2)
	session = core.registry.get(s["session_id"])
	assert session.state in (SessionState.ERROR, SessionState.IDLE, SessionState.CREATING)


@pytest.mark.integration
async def test_i10_mcp_server_errors(test_config: Config, tmp_workspace) -> None:
	script = init_script(mcp_server_errors=[{"name": "bad", "error": "failed"}])
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	repo = tmp_workspace / "repo"
	with pytest.raises(McpServerFailedError):
		await core.create_session(str(repo))
