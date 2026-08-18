"""Integration tests for fork and resume."""

from __future__ import annotations

import pytest

from broker.config import Config
from broker.core import BrokerCore
from broker.errors import SessionBusyError, SessionNotFoundError
from broker.registry import SessionState
from tests.conftest import FakeSDKClient, complete_turn_script


@pytest.mark.integration
async def test_i37_fork_idle(test_config: Config, tmp_workspace) -> None:
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	parent = await core.create_session(str(repo))
	await core.send(parent["session_id"], "hello", wait_ms=5000)
	fork = await core.fork_session(parent["session_id"])
	assert fork["session_id"] != parent["session_id"]
	assert fork.get("parent_session_id") == parent["session_id"]
	assert core.registry.get(parent["session_id"]).state == SessionState.IDLE


@pytest.mark.integration
async def test_i38_fork_busy(test_config: Config, tmp_workspace) -> None:
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	parent = await core.create_session(str(repo))
	session = core.registry.get(parent["session_id"])
	session.state = SessionState.RUNNING
	session.current_turn = type("T", (), {"turn_id": "t1", "state": "running"})()
	with pytest.raises(SessionBusyError):
		await core.fork_session(parent["session_id"])


@pytest.mark.integration
async def test_i39_fork_diverge(test_config: Config, tmp_workspace) -> None:
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	parent = await core.create_session(str(repo))
	fork = await core.fork_session(parent["session_id"])
	await core.send(parent["session_id"], "parent", wait_ms=5000)
	await core.send(fork["session_id"], "fork", wait_ms=5000)
	p_cost = core.registry.get(parent["session_id"]).cost.total_usd
	f_cost = core.registry.get(fork["session_id"]).cost.total_usd
	assert parent["session_id"] != fork["session_id"]


@pytest.mark.integration
async def test_i40_resume_after_close(test_config: Config, tmp_workspace) -> None:
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	s = await core.create_session(str(repo))
	await core.send(s["session_id"], "hello", wait_ms=5000)
	sid = s["session_id"]
	await core.close_session(sid)
	resumed = await core.create_session(str(repo), resume_from=sid)
	assert resumed["session_id"] != sid


@pytest.mark.integration
async def test_i41_resume_unknown(test_config: Config, tmp_workspace) -> None:
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(complete_turn_script()))
	repo = tmp_workspace / "repo"
	with pytest.raises(SessionNotFoundError):
		await core.create_session(str(repo), resume_from="00000000-0000-0000-0000-000000000000")
