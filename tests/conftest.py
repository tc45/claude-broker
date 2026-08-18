"""Shared test fixtures."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from broker.config import Config, set_auth_probe_result
from broker.core import BrokerCore


def config_with(base: Config, **kwargs: object) -> Config:
	return replace(base, **kwargs)


@dataclass
class ToolUseRequest:
	tool: str
	input: dict[str, Any]
	tool_use_id: str = "toolu_test"


@dataclass
class TextBlock:
	text: str


@dataclass
class SystemMessage:
	subtype: str
	data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantMessage:
	content: list[Any] = field(default_factory=list)


@dataclass
class ResultMessage:
	subtype: str = "success"
	is_error: bool = False
	num_turns: int = 1
	session_id: str = "test"
	total_cost_usd: float = 0.01
	duration_ms: int = 100
	duration_api_ms: int = 90
	result: str = "Done."
	stop_reason: str = "end_turn"
	usage: dict[str, Any] = field(default_factory=dict)
	model_usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolPermissionContext:
	suggestions: list[Any] = field(default_factory=list)


ScriptItem = Any | tuple[Any, float] | tuple[str, Any]


class FakeSDKClient:
	"""Fake ClaudeSDKClient driven by a scripted message sequence."""

	def __init__(
		self,
		options: Any = None,
		session: Any = None,
		script: list[ScriptItem] | None = None,
	) -> None:
		self.options = options
		self._session = session
		self._script = list(script or [])
		self._turn_script: list[ScriptItem] = []
		self._queue: asyncio.Queue[Any] = asyncio.Queue()
		self._connected = False
		self._released = asyncio.Event()
		self._released.set()
		self._interrupt_event = asyncio.Event()
		self._pending_query: str | None = None

	@classmethod
	def factory(cls, script: list[ScriptItem]) -> Callable[..., FakeSDKClient]:
		def _factory(options: Any, session: Any) -> FakeSDKClient:
			client = cls(options=options, session=session, script=script)
			return client

		return _factory

	async def connect(self) -> None:
		self._connected = True
		# Feed init messages immediately
		for item in self._script:
			if isinstance(item, SystemMessage):
				await self._queue.put(item)
			else:
				break
		self._turn_script = [
			item for item in self._script if not (isinstance(item, SystemMessage))
		]

	async def _feed_script(self) -> None:
		for item in self._turn_script:
			if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], (int, float)):
				await asyncio.sleep(item[0])
				msg = item[1]
			elif isinstance(item, tuple) and item[0] == "delay":
				await asyncio.sleep(item[1])
				continue
			elif isinstance(item, tuple) and item[0] == "raise":
				raise item[1]
			elif isinstance(item, tuple) and item[0] == "block":
				await self._released.wait()
				continue
			else:
				msg = item

			if isinstance(msg, ToolUseRequest):
				cb = getattr(self.options, "can_use_tool", None)
				if cb:
					ctx = ToolPermissionContext(suggestions=[{"type": "addRules"}])
					result = await cb(msg.tool, msg.input, ctx)
					try:
						from claude_agent_sdk import PermissionResultDeny
						is_deny = isinstance(result, PermissionResultDeny)
					except ImportError:
						is_deny = getattr(result, "behavior", None) == "deny"
					if is_deny and getattr(result, "interrupt", False):
						break
				continue

			await self._queue.put(msg)

	async def receive_messages(self) -> AsyncIterator[Any]:
		while True:
			try:
				msg = await asyncio.wait_for(self._queue.get(), timeout=0.1)
				yield msg
			except TimeoutError:
				if self._interrupt_event.is_set():
					self._interrupt_event.clear()
					return
				if not self._connected:
					return

	async def query(self, prompt: str) -> None:
		self._pending_query = prompt
		asyncio.create_task(self._feed_script())

	async def interrupt(self) -> None:
		self._interrupt_event.set()
		if self._session and self._session.current_turn:
			self._session.current_turn.state = "interrupted"

	async def disconnect(self) -> None:
		self._connected = False

	def release(self) -> None:
		self._released.set()

	def block(self) -> None:
		self._released.clear()

	async def __aenter__(self) -> FakeSDKClient:
		await self.connect()
		return self

	async def __aexit__(self, *args: Any) -> None:
		await self.disconnect()


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
	state = tmp_path / "state"
	state.mkdir()
	return state


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
	ws = tmp_path / "workspace"
	ws.mkdir()
	(ws / "repo").mkdir()
	return ws


@pytest.fixture
def test_config(tmp_state_dir: Path, tmp_workspace: Path) -> Config:
	os.environ["BROKER_SKIP_AUTH_PROBE"] = "1"
	os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "test-token"
	os.environ.pop("ANTHROPIC_API_KEY", None)
	os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
	policy_file = Path(__file__).parent.parent / "policies" / "default.yaml"
	return Config(
		transport="http",
		host="127.0.0.1",
		port=8787,
		auth_token="test-auth",
		oauth_token="test-token",
		workspace_roots=(tmp_workspace,),
		state_dir=tmp_state_dir,
		default_policy="reviewed",
		policy_file=policy_file,
		max_sessions=8,
		session_idle_ttl=3600,
		session_retain=86400,
		event_memory_limit=5000,
		permission_timeout=2,
		global_budget_usd=100.0,
		default_budget_usd=2.0,
		allow_api_billing=False,
		cli_path=Path("/usr/local/bin/claude"),
		log_level="WARNING",
		cooldown_base=30.0,
		cooldown_max=900.0,
		budget_window="monthly",
		passthrough_extra_args=(),
		billing_mode="subscription",
	)


@pytest.fixture
def broker(test_config: Config) -> BrokerCore:
	set_auth_probe_result({
		"method": "CLAUDE_CODE_OAUTH_TOKEN",
		"billing": "subscription",
	})
	return BrokerCore(test_config)


def init_script(**overrides: Any) -> list[Any]:
	data = {
		"model": "claude-opus-5",
		"tools": ["Read", "Bash"],
		"mcp_servers": [],
		"mcp_server_errors": [],
		"capabilities": ["interrupt_receipt_v1"],
	}
	data.update(overrides)
	return [SystemMessage(subtype="init", data=data)]


def complete_turn_script(text: str = "Done.", cost: float = 0.01) -> list[Any]:
	return init_script() + [
		AssistantMessage(content=[TextBlock(text="Working.")]),
		ResultMessage(total_cost_usd=cost, result=text),
	]
