"""Regressions for the silent-deny hang.

A guard denial used to be answered to the SDK and nowhere else: no event, no
wakeup. The model was told, but every observer of the session saw a tool_use
with no outcome, and the run then stalled on whatever fallback tool the model
reached for next. Worse, a session in a running state could not be closed at
all — session_close tripped the registry invariant and raised.
"""

from __future__ import annotations

import pytest

from broker.config import Config
from broker.core import BrokerCore
from broker.registry import SessionState
from tests.conftest import (
	AssistantMessage,
	FakeSDKClient,
	ResultMessage,
	TextBlock,
	ToolUseRequest,
	init_script,
)


def _events(core: BrokerCore, session_id: str) -> list[dict]:
	events, _, _ = core.registry.get(session_id).event_log.poll(cursor=0, limit=1000)
	return events


@pytest.mark.integration
async def test_guard_deny_is_visible_in_the_event_log(
	test_config: Config, tmp_workspace
) -> None:
	outside = tmp_workspace / "outside.txt"  # sibling of the workspace, not inside it
	script = init_script() + [
		ToolUseRequest(tool="Write", input={"file_path": str(outside), "content": "x"}),
		ResultMessage(),
	]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))

	await core.send(s["session_id"], "write outside the workspace", wait_ms=5000)

	decisions = [
		e for e in _events(core, s["session_id"]) if e["type"] == "permission_decision"
	]
	assert decisions, "a guard denial must reach the event log"
	decision = decisions[0]
	assert decision["decision"] == "deny"
	assert decision["decided_by"] == "guard"
	assert str(outside) in decision["reason"]
	assert decision["tool"] == "Write"
	# The turn must still finish: a deny is an answer, not a dead end.
	assert core.registry.get(s["session_id"]).state == SessionState.IDLE


@pytest.mark.integration
async def test_callback_failure_denies_instead_of_wedging(
	test_config: Config, tmp_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""An exception in the callback must not become a control-protocol error.

	The SDK answers a raising can_use_tool with an error response; the CLI then
	never produces a tool_result and the session sits in RUNNING forever.
	"""
	from broker.permissions import Policy

	def boom(*args: object, **kwargs: object) -> None:
		raise RuntimeError("policy engine exploded")

	monkeypatch.setattr(Policy, "evaluate", boom)

	script = init_script() + [
		ToolUseRequest(tool="Read", input={"file_path": str(tmp_workspace / "repo" / "a")}),
		ResultMessage(),
	]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))

	await core.send(s["session_id"], "read something", wait_ms=5000)

	session = core.registry.get(s["session_id"])
	assert session.state == SessionState.IDLE, "a broker-side failure must not wedge the turn"
	decisions = [e for e in _events(core, s["session_id"]) if e["type"] == "permission_decision"]
	assert decisions and decisions[0]["decided_by"] == "broker_error"
	assert "policy engine exploded" in decisions[0]["reason"]


@pytest.mark.integration
async def test_close_while_awaiting_permission(test_config: Config, tmp_workspace) -> None:
	script = init_script() + [
		ToolUseRequest(tool="Bash", input={"command": "curl http://example.com"})
	]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	await core.send(s["session_id"], "fetch", wait_ms=3000)
	assert core.registry.get(s["session_id"]).state == SessionState.AWAITING_PERMISSION

	result = await core.close_session(s["session_id"])

	assert result["state"] == "closed"
	assert core.registry.get(s["session_id"]).current_turn is None
	assert not core.permissions.list_pending(s["session_id"])


@pytest.mark.integration
async def test_close_while_running(test_config: Config, tmp_workspace) -> None:
	# No ResultMessage: the turn never ends on its own, as with a hung CLI.
	script = init_script() + [AssistantMessage(content=[TextBlock(text="thinking...")])]
	core = BrokerCore(test_config, client_factory=FakeSDKClient.factory(script))
	s = await core.create_session(str(tmp_workspace / "repo"))
	await core.send(s["session_id"], "work", wait_ms=300)
	session = core.registry.get(s["session_id"])
	assert session.state == SessionState.RUNNING
	assert session.current_turn is not None

	result = await core.close_session(s["session_id"])

	assert result["state"] == "closed"
	assert session.current_turn is None
	assert session.turn_history[-1].state == "interrupted"
