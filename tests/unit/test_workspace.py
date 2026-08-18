"""Unit tests for workspace validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from broker.errors import InvalidArgumentError, WorkspaceInvalidError
from broker.workspace import validate


@pytest.fixture
def ws_root(tmp_path: Path) -> Path:
	root = tmp_path / "workspace"
	root.mkdir()
	repo = root / "repo"
	repo.mkdir()
	return root


def test_u11_valid_path(ws_root: Path) -> None:
	result = validate(str(ws_root / "repo"), (ws_root,))
	assert result == (ws_root / "repo").resolve()


def test_u12_outside_root(ws_root: Path) -> None:
	outside = Path("C:/outside_broker_test") if os.name == "nt" else Path("/etc/passwd")
	with pytest.raises(WorkspaceInvalidError):
		validate(str(outside), (ws_root,))


def test_u13_traversal(ws_root: Path) -> None:
	with pytest.raises(WorkspaceInvalidError):
		validate(str(ws_root / ".." / "etc"), (ws_root,))


def test_u14_symlink_escape(ws_root: Path, tmp_path: Path) -> None:
	if os.name == "nt":
		pytest.skip("symlink test requires admin on Windows")
	link = ws_root / "link"
	link.symlink_to("/etc")
	with pytest.raises(WorkspaceInvalidError):
		validate(str(link), (ws_root,))


def test_u15_nonexistent(ws_root: Path) -> None:
	with pytest.raises(WorkspaceInvalidError):
		validate(str(ws_root / "missing"), (ws_root,))


def test_u16_not_directory(ws_root: Path) -> None:
	f = ws_root / "file.txt"
	f.write_text("x")
	with pytest.raises(WorkspaceInvalidError):
		validate(str(f), (ws_root,))


def test_u17_relative_path(ws_root: Path) -> None:
	with pytest.raises(InvalidArgumentError):
		validate("repo", (ws_root,))
