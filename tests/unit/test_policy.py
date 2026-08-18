"""Unit tests for policy engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from broker.permissions import Policy, PolicyEngine, PolicyLoadError, PolicyRule, _matches


@pytest.fixture
def engine() -> PolicyEngine:
	policy_file = Path(__file__).parent.parent.parent / "policies" / "default.yaml"
	return PolicyEngine(policy_file)


class FakeSession:
	def __init__(self, workspace: Path, additional: list[str] | None = None) -> None:
		self.workspace = workspace
		self.additional_directories = additional or []


def test_u18_first_match_wins(engine: PolicyEngine) -> None:
	policy = Policy(
		name="test",
		description="",
		default="deny",
		rules=[
			PolicyRule(match="Read", decision="allow"),
			PolicyRule(match="Write", decision="deny"),
		],
	)
	d = policy.evaluate("Read", {})
	assert d.verdict == "allow"


def test_u19_space_significance() -> None:
	assert _matches("Bash(git diff *)", "Bash", {"command": "git diff HEAD"})
	assert not _matches("Bash(git diff *)", "Bash", {"command": "git diff-index HEAD"})


def test_u20_git_diff_matches() -> None:
	assert _matches("Bash(git diff *)", "Bash", {"command": "git diff HEAD"})


def test_u21_shadowing_rejected() -> None:
	from broker.permissions import PolicyRule, _detect_shadowing

	with pytest.raises(PolicyLoadError, match="shadows"):
		_detect_shadowing([
			PolicyRule(match="Bash", decision="ask"),
			PolicyRule(match="Bash(rm *)", decision="deny"),
		])


def test_u22_mcp_wildcard() -> None:
	assert _matches("mcp__*", "mcp__github__create_issue", {})


def test_u23_default_applies(engine: PolicyEngine) -> None:
	policy = Policy(name="t", description="", default="deny", rules=[])
	assert policy.evaluate("UnknownTool", {}).verdict == "deny"


def test_u24_workspace_write_inside(tmp_path: Path, engine: PolicyEngine) -> None:
	ws = tmp_path / "ws"
	ws.mkdir()
	f = ws / "a.py"
	f.write_text("x")
	policy = engine.get("reviewed")
	session = FakeSession(ws)
	d = policy.evaluate("Edit", {"file_path": str(f)}, session)
	assert d.verdict == "allow"


def test_u25_workspace_write_outside(tmp_path: Path, engine: PolicyEngine) -> None:
	ws = tmp_path / "ws"
	ws.mkdir()
	policy = engine.get("reviewed")
	session = FakeSession(ws)
	d = policy.evaluate("Edit", {"file_path": "/etc/passwd"}, session)
	assert d.verdict == "deny"


def test_u26_symlink_escape(tmp_path: Path, engine: PolicyEngine) -> None:
	import os
	if os.name == "nt":
		pytest.skip("symlink")
	ws = tmp_path / "ws"
	ws.mkdir()
	link = ws / "escape"
	link.symlink_to("/etc")
	policy = engine.get("reviewed")
	session = FakeSession(ws)
	d = policy.evaluate("Edit", {"file_path": str(link / "passwd")}, session)
	assert d.verdict == "deny"


def test_u27_no_push(engine: PolicyEngine) -> None:
	policy = engine.get("reviewed")
	d = policy.evaluate("Bash", {"command": "git push origin main"}, FakeSession(Path("/ws")))
	assert d.verdict == "ask"


def test_u28_readonly_write(engine: PolicyEngine) -> None:
	policy = engine.get("readonly")
	assert policy.evaluate("Write", {}).verdict == "deny"


def test_u29_autonomous_sudo(engine: PolicyEngine) -> None:
	policy = engine.get("autonomous")
	d = policy.evaluate("Bash", {"command": "sudo rm file"}, FakeSession(Path("/ws")))
	assert d.verdict == "deny"
