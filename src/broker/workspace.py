"""Workspace path validation and traversal defence."""

from __future__ import annotations

from pathlib import Path

from broker.errors import InvalidArgumentError, WorkspaceInvalidError


def validate(path: str | Path, roots: tuple[Path, ...]) -> Path:
	"""Validate that path is absolute and resolves under an allowed root."""
	p = Path(path)
	if not p.is_absolute():
		raise InvalidArgumentError(f"Workspace path must be absolute, got {path!r}")

	try:
		resolved = p.resolve()
	except OSError as exc:
		raise WorkspaceInvalidError(str(path), str(exc)) from exc

	if not resolved.exists():
		raise WorkspaceInvalidError(str(path), "path does not exist")
	if not resolved.is_dir():
		raise WorkspaceInvalidError(str(path), "path is not a directory")

	for root in roots:
		try:
			root_resolved = root.resolve()
		except OSError:
			continue
		try:
			resolved.relative_to(root_resolved)
			return resolved
		except ValueError:
			continue

	raise WorkspaceInvalidError(str(path), "outside allowed workspace roots")


def list_roots(roots: tuple[Path, ...]) -> list[dict[str, object]]:
	"""Enumerate workspace roots and their entries."""
	result: list[dict[str, object]] = []
	for root in roots:
		entries: list[str] = []
		if root.exists() and root.is_dir():
			try:
				entries = sorted(e.name for e in root.iterdir())
			except OSError:
				entries = []
		result.append({"path": str(root), "writable": True, "entries": entries})
	return result
