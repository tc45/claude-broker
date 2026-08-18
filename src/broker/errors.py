"""Broker error taxonomy mapped to MCP tool errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BrokerError(Exception):
	"""Base broker error with taxonomy code."""

	code: str
	message: str
	retryable: bool = False
	details: dict[str, Any] = field(default_factory=dict)

	def __str__(self) -> str:
		return self.message

	def to_dict(self) -> dict[str, Any]:
		return {
			"error": {
				"code": self.code,
				"message": self.message,
				"retryable": self.retryable,
				"details": self.details,
			}
		}


class InvalidArgumentError(BrokerError):
	def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
		super().__init__("INVALID_ARGUMENT", message, retryable=False, details=details or {})


class SessionNotFoundError(BrokerError):
	def __init__(self, session_id: str) -> None:
		super().__init__(
			"SESSION_NOT_FOUND",
			f"Session {session_id!r} not found",
			retryable=False,
			details={"session_id": session_id},
		)


class SessionBusyError(BrokerError):
	def __init__(self, session_id: str) -> None:
		super().__init__(
			"SESSION_BUSY",
			f"Session {session_id!r} has a turn in flight",
			retryable=True,
			details={"session_id": session_id},
		)


class SessionTerminalError(BrokerError):
	def __init__(self, session_id: str, state: str) -> None:
		super().__init__(
			"SESSION_TERMINAL",
			f"Session {session_id!r} is in terminal state {state!r}",
			retryable=False,
			details={"session_id": session_id, "state": state},
		)


class SessionLimitReachedError(BrokerError):
	def __init__(self, max_sessions: int) -> None:
		super().__init__(
			"SESSION_LIMIT_REACHED",
			f"Maximum concurrent sessions ({max_sessions}) reached",
			retryable=True,
			details={"max_sessions": max_sessions},
		)


class WorkspaceInvalidError(BrokerError):
	def __init__(self, path: str, reason: str) -> None:
		super().__init__(
			"WORKSPACE_INVALID",
			f"Workspace {path!r} is invalid: {reason}",
			retryable=False,
			details={"path": path, "reason": reason},
		)


class PermissionNotFoundError(BrokerError):
	def __init__(self, request_id: str) -> None:
		super().__init__(
			"PERMISSION_NOT_FOUND",
			f"Permission request {request_id!r} not found or already resolved",
			retryable=False,
			details={"request_id": request_id},
		)


class PermissionExpiredError(BrokerError):
	def __init__(self, request_id: str) -> None:
		super().__init__(
			"PERMISSION_EXPIRED",
			f"Permission request {request_id!r} has expired",
			retryable=False,
			details={"request_id": request_id},
		)


class BudgetExceededError(BrokerError):
	def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
		super().__init__("BUDGET_EXCEEDED", message, retryable=False, details=details or {})


class RateLimitedError(BrokerError):
	def __init__(self, retry_after_seconds: float) -> None:
		super().__init__(
			"RATE_LIMITED",
			"Rate limit cooldown active",
			retryable=True,
			details={"retry_after_seconds": retry_after_seconds},
		)


class AuthFailedError(BrokerError):
	def __init__(self, message: str) -> None:
		super().__init__("AUTH_FAILED", message, retryable=False)


class McpServerFailedError(BrokerError):
	def __init__(self, errors: list[Any]) -> None:
		super().__init__(
			"MCP_SERVER_FAILED",
			"One or more MCP servers failed to load",
			retryable=False,
			details={"mcp_server_errors": errors},
		)


class InterruptTimeoutError(BrokerError):
	def __init__(self, session_id: str) -> None:
		super().__init__(
			"INTERRUPT_TIMEOUT",
			f"Interrupt not acknowledged within 10s for session {session_id!r}",
			retryable=False,
			details={"session_id": session_id},
		)


class CliUnavailableError(BrokerError):
	def __init__(self, message: str) -> None:
		super().__init__("CLI_UNAVAILABLE", message, retryable=False)


class InternalError(BrokerError):
	def __init__(self, message: str = "Internal error") -> None:
		super().__init__("INTERNAL", message, retryable=True)


class ConfigError(Exception):
	"""Fatal configuration error at startup."""

	def __init__(self, message: str) -> None:
		self.message = message
		super().__init__(message)
