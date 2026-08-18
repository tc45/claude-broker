"""Additional unit tests for coverage gaps."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from broker.config import decode_token_expiry
from broker.permissions import _apply_guard
from broker.registry import Session
from broker.workspace import list_roots


def test_decode_token_expiry_from_jwt() -> None:
	exp = int((datetime.now(tz=UTC) + timedelta(days=100)).timestamp())
	payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
	token = f"hdr.{payload}.sig"
	info = decode_token_expiry(token)
	assert info["days_remaining"] is not None


def test_decode_token_expiry_invalid() -> None:
	assert decode_token_expiry("not-a-jwt")["token_expires_at"] is None


def test_list_roots(tmp_path: Path) -> None:
	root = tmp_path / "ws"
	root.mkdir()
	(root / "project").mkdir()
	roots = list_roots((root,))
	assert roots[0]["entries"] == ["project"]


def test_no_secrets_guard(tmp_path: Path) -> None:
	from broker.event_log import EventLog
	from broker.permissions import RuleDecision

	session = Session("s", tmp_path / "ws", EventLog(tmp_path / "ev"))
	decision = RuleDecision(verdict="allow", rule_text="allow: Read")
	result = _apply_guard(decision, "no_secrets", "Read", {"file_path": str(tmp_path / "ws" / ".env")}, session)
	assert result.verdict == "ask"


def test_config_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	monkeypatch.setenv("BROKER_WORKSPACE_ROOTS", str(tmp_path))
	monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
	from broker.config import Config

	cfg = Config.from_env()
	assert cfg.oauth_token == "tok"


def test_normalise_blocks() -> None:
	from dataclasses import dataclass

	from broker.normalise import normalise
	from tests.conftest import TextBlock

	events = normalise(type("AssistantMessage", (), {"content": [TextBlock("hi")]})(), 0)
	assert events[0].type == "assistant_text"

	@dataclass
	class ThinkingBlock:
		thinking: str = "thought"

	events = normalise(type("AssistantMessage", (), {"content": [ThinkingBlock()]})(), 0)
	assert events[0].type == "thinking"

	@dataclass
	class ToolUseBlock:
		name: str = "Read"
		id: str = "t1"
		input: dict = None

	events = normalise(type("AssistantMessage", (), {"content": [ToolUseBlock()]})(), 0)
	assert events[0].type == "tool_use"


def test_reaper_instantiation() -> None:
	from broker.config import Config
	from broker.core import BrokerCore
	from broker.reaper import Reaper

	cfg = Config(
		transport="http", host="127.0.0.1", port=8787, auth_token=None,
		oauth_token="x", workspace_roots=(Path("/w"),), state_dir=Path("/s"),
		default_policy="reviewed", policy_file=Path("policies/default.yaml"),
		max_sessions=8, session_idle_ttl=3600, session_retain=86400,
		event_memory_limit=5000, permission_timeout=300, global_budget_usd=None,
		default_budget_usd=2.0, allow_api_billing=False,
		cli_path=Path("/usr/local/bin/claude"), log_level="INFO",
		cooldown_base=30.0, cooldown_max=900.0, budget_window="monthly",
		passthrough_extra_args=(), billing_mode="subscription",
	)
	core = BrokerCore(cfg)
	reaper = Reaper(core)
	assert reaper._interval == 60.0
