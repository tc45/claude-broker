"""Integration tests for MCP facade."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from broker.config import Config, set_auth_probe_result
from broker.core import BrokerCore
from broker.mcp_server import (
	SessionCreateArgs,
	SessionPollArgs,
	SessionSendArgs,
	create_mcp_server,
	verify_bearer,
)
from tests.conftest import config_with


@pytest.fixture
def mcp_core(test_config: Config) -> BrokerCore:
	set_auth_probe_result({"method": "CLAUDE_CODE_OAUTH_TOKEN", "billing": "subscription"})
	return BrokerCore(test_config)


@pytest.mark.integration
def test_i47_schemas_validate() -> None:
	SessionCreateArgs(workspace="/workspace/repo")
	SessionSendArgs(session_id="s", prompt="hi")
	SessionPollArgs(session_id="s")


@pytest.mark.integration
def test_i47b_tool_schemas_are_flat(test_config: Config, mcp_core: BrokerCore) -> None:
	mcp = create_mcp_server(mcp_core, test_config)
	tools = mcp._tool_manager.list_tools()
	create_tool = next(tool for tool in tools if tool.name == "session_create")
	schema = create_tool.parameters
	assert "kwargs" not in schema.get("properties", {})
	assert "workspace" in schema.get("properties", {})


@pytest.mark.integration
def test_i48_unknown_field_rejected() -> None:
	with pytest.raises(Exception):
		SessionCreateArgs(workspace="/w", unknown_field="x")  # type: ignore[call-arg]


@pytest.mark.integration
def test_i49_wait_ms_range() -> None:
	with pytest.raises(Exception):
		SessionSendArgs(session_id="s", prompt="hi", wait_ms=-1)
	with pytest.raises(Exception):
		SessionSendArgs(session_id="s", prompt="hi", wait_ms=60001)


@pytest.mark.integration
def test_i50_missing_bearer() -> None:
	assert not verify_bearer(None, "secret-token")


@pytest.mark.integration
def test_i50b_missing_bearer_http(test_config: Config, mcp_core: BrokerCore) -> None:
	mcp = create_mcp_server(mcp_core, test_config)
	with TestClient(mcp.streamable_http_app(), raise_server_exceptions=False) as client:
		response = client.post(
			"/mcp",
			content=json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}}),
			headers={"Content-Type": "application/json"},
		)
	assert response.status_code == 401


@pytest.mark.integration
def test_i51_wrong_bearer() -> None:
	assert not verify_bearer("Bearer wrong", "secret-token")
	assert verify_bearer("Bearer secret-token", "secret-token")


@pytest.mark.integration
def test_i51b_wrong_bearer_http(test_config: Config, mcp_core: BrokerCore) -> None:
	mcp = create_mcp_server(mcp_core, test_config)
	with TestClient(mcp.streamable_http_app(), raise_server_exceptions=False) as client:
		response = client.post(
			"/mcp",
			content=json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}}),
			headers={
				"Content-Type": "application/json",
				"Authorization": "Bearer wrong-token",
			},
		)
	assert response.status_code == 401


@pytest.mark.integration
def test_i52_broker_error_mapping() -> None:
	from broker.errors import SessionBusyError

	err = SessionBusyError("s1")
	d = err.to_dict()
	assert d["error"]["code"] == "SESSION_BUSY"
	assert "traceback" not in str(d).lower()


@pytest.mark.integration
def test_i53_api_billing_degraded(test_config: Config) -> None:
	cfg = config_with(test_config, billing_mode="api", allow_api_billing=True)
	core = BrokerCore(cfg)
	status = core.broker_status()
	assert status["health"] == "degraded"
	assert status["auth"]["billing"] == "api"
