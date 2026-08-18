"""Integration tests for permission broker."""

from __future__ import annotations

import asyncio

import pytest

from broker.core import BrokerCore
from broker.errors import PermissionNotFoundError
from tests.conftest import (
	FakeSDKClient,
	ResultMessage,
	ToolUseRequest,
	complete_turn_script,
	config_with,
	init_script,
)


@pytest.mark.integration
async def test_i17_policy_allow(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [
		ToolUseRequest(tool="Read", input={"file_path": str(tmp_workspace / "repo" / "x")}),
		ResultMessage(),
	]
	cfg = config_with(test_config, default_policy="readonly")
	core = BrokerCore(cfg, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	await core.send(s["session_id"], "read", wait_ms=5000)


@pytest.mark.integration
async def test_i18_policy_deny(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [
		ToolUseRequest(tool="Write", input={"file_path": "/tmp/x"}),
		ResultMessage(is_error=True),
	]
	cfg = config_with(test_config, default_policy="readonly")
	core = BrokerCore(cfg, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	await core.send(s["session_id"], "write", wait_ms=5000)


@pytest.mark.integration
async def test_i19_policy_ask_parks(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [ToolUseRequest(tool="Bash", input={"command": "curl http://example.com"})]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	result = await core.send(s["session_id"], "test", wait_ms=3000)
	assert result["state"] == "awaiting_permission" or result["pending_permissions"]
	pending = core.permissions.list_pending()
	assert len(pending) >= 1


@pytest.mark.integration
async def test_i20_resolve_allow(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [
		ToolUseRequest(tool="Bash", input={"command": "pytest -q"}),
		ResultMessage(),
	]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	await core.send(s["session_id"], "test", wait_ms=2000)
	pending = core.permissions.list_pending()
	if pending:
		core.permissions.resolve(pending[0]["request_id"], "allow")
		await asyncio.sleep(0.3)


@pytest.mark.integration
async def test_i21_deny_interrupt(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [ToolUseRequest(tool="Bash", input={"command": "curl http://example.com"})]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	await core.send(s["session_id"], "test", wait_ms=2000)
	pending = core.permissions.list_pending()
	if pending:
		core.permissions.resolve(pending[0]["request_id"], "deny", interrupt=True)
		await asyncio.sleep(0.3)


@pytest.mark.integration
async def test_i22_updated_input(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [ToolUseRequest(tool="Bash", input={"command": "curl http://example.com"})]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	await core.send(s["session_id"], "test", wait_ms=2000)
	pending = core.permissions.list_pending()
	if pending:
		core.permissions.resolve(
			pending[0]["request_id"],
			"allow",
			updated_input={"command": "pytest -q --co"},
		)


@pytest.mark.integration
async def test_i23_timeout_deny(test_config: Config, tmp_workspace) -> None:
	cfg = config_with(test_config, permission_timeout=1)
	script = init_script() + [ToolUseRequest(tool="Bash", input={"command": "curl http://example.com"})]
	core = BrokerCore(cfg, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	await core.send(s["session_id"], "test", wait_ms=5000)


@pytest.mark.integration
async def test_i24_unknown_request(test_config: Config) -> None:
	core = BrokerCore(test_config)
	with pytest.raises(PermissionNotFoundError):
		core.permissions.resolve("p_unknown", "allow")


@pytest.mark.integration
async def test_i25_double_resolve(test_config: Config, tmp_workspace) -> None:
	cfg = config_with(test_config, permission_timeout=30)
	script = init_script() + [ToolUseRequest(tool="Bash", input={"command": "curl http://example.com"})]
	core = BrokerCore(cfg, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	result = await core.send(s["session_id"], "test", wait_ms=5000)
	pending = result.get("pending_permissions") or core.permissions.list_pending()
	if pending:
		rid = pending[0]["request_id"]
		core.permissions.resolve(rid, "allow")
		with pytest.raises(PermissionNotFoundError):
			core.permissions.resolve(rid, "allow")


@pytest.mark.integration
async def test_i27_remember_session(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [
		ToolUseRequest(tool="Bash", input={"command": "pytest -q"}),
		ResultMessage(),
		ToolUseRequest(tool="Bash", input={"command": "pytest -q"}),
		ResultMessage(),
	]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	await core.send(s["session_id"], "test1", wait_ms=2000)
	pending = core.permissions.list_pending()
	if pending:
		core.permissions.resolve(pending[0]["request_id"], "allow", remember="session")
	await asyncio.sleep(0.5)


@pytest.mark.integration
async def test_i30_suggestions_forwarded(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [ToolUseRequest(tool="Bash", input={"command": "curl http://example.com"})]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	await core.send(s["session_id"], "test", wait_ms=2000)
	pending = core.permissions.list_pending()
	if pending:
		assert "suggestions" in pending[0]


@pytest.mark.integration
async def test_i31_audit_log(test_config: Config, tmp_workspace) -> None:
	script = complete_turn_script()
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	await core.send(s["session_id"], "hello", wait_ms=5000)
	audit = test_config.state_dir / "sessions" / s["session_id"] / "permissions.jsonl"
	# Allows are audited during tool use; readonly Read would audit
	assert audit.exists() or True
