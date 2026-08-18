"""MCP facade — thin translation layer over BrokerCore."""

from __future__ import annotations

import hmac
from typing import Any

import structlog
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyUrl, BaseModel, ConfigDict, Field

from broker.config import Config
from broker.core import BrokerCore
from broker.errors import BrokerError, InvalidArgumentError
from broker.workspace import list_roots

logger = structlog.get_logger()


class StrictModel(BaseModel):
	model_config = ConfigDict(extra="forbid")


class SessionCreateArgs(StrictModel):
	workspace: str
	model: str | None = None
	effort: str | None = None
	system_prompt_append: str | None = Field(None, max_length=8192)
	permission_policy: str | None = None
	allowed_tools: list[str] = Field(default_factory=list)
	disallowed_tools: list[str] = Field(default_factory=list)
	max_budget_usd: float | None = Field(None, ge=0.01, le=100.0)
	max_turns: int | None = Field(None, ge=1, le=200)
	mcp_servers: dict[str, Any] = Field(default_factory=dict)
	agents: dict[str, Any] | None = None
	additional_directories: list[str] = Field(default_factory=list)
	setting_sources: list[str] = Field(default_factory=list)
	resume_from: str | None = None
	fork_from: str | None = None
	label: str | None = Field(None, max_length=120)
	idle_ttl_seconds: int | None = Field(None, ge=60, le=86400)


class SessionSendArgs(StrictModel):
	session_id: str
	prompt: str = Field(..., min_length=1, max_length=1_000_000)
	wait_ms: int = Field(0, ge=0, le=60000)


class SessionPollArgs(StrictModel):
	session_id: str
	cursor: int = Field(0, ge=0)
	wait_ms: int = Field(0, ge=0, le=60000)
	limit: int = Field(200, ge=1, le=1000)
	include: list[str] | None = None


class SessionInterruptArgs(StrictModel):
	session_id: str


class SessionForkArgs(StrictModel):
	session_id: str
	label: str | None = None
	model: str | None = None
	permission_policy: str | None = None
	max_budget_usd: float | None = None
	workspace: str | None = None


class SessionCloseArgs(StrictModel):
	session_id: str
	reason: str | None = None


class SessionListArgs(StrictModel):
	state: list[str] | None = None
	include_closed: bool = False
	limit: int = Field(50, ge=1, le=500)


class SessionTranscriptArgs(StrictModel):
	session_id: str
	format: str = "json"
	from_cursor: int = Field(0, ge=0)
	to_cursor: int | None = Field(None, ge=0)
	include: list[str] | None = None


class PermissionPendingArgs(StrictModel):
	session_id: str | None = None


class PermissionResolveArgs(StrictModel):
	request_id: str
	decision: str
	reason: str | None = None
	updated_input: dict[str, Any] | None = None
	interrupt: bool = False
	remember: str = "none"


class StaticTokenVerifier:
	"""Validate bearer tokens against BROKER_AUTH_TOKEN."""

	def __init__(self, expected_token: str) -> None:
		self._expected_token = expected_token

	async def verify_token(self, token: str) -> AccessToken | None:
		if hmac.compare_digest(token, self._expected_token):
			return AccessToken(token=token, client_id="broker", scopes=[])
		return None


def _auth_base_url(config: Config) -> str:
	host = "127.0.0.1" if config.host == "0.0.0.0" else config.host
	return f"http://{host}:{config.port}"


def create_mcp_server(core: BrokerCore, config: Config) -> FastMCP:
	"""Create FastMCP server with all 12 broker tools."""
	auth: AuthSettings | None = None
	token_verifier: StaticTokenVerifier | None = None
	if config.auth_token:
		base_url = _auth_base_url(config)
		auth = AuthSettings(
			issuer_url=AnyUrl(base_url),
			resource_server_url=AnyUrl(f"{base_url}/mcp"),
		)
		token_verifier = StaticTokenVerifier(config.auth_token)

	mcp = FastMCP(
		"claude-broker",
		host=config.host,
		port=config.port,
		log_level=config.log_level,  # type: ignore[arg-type]
		auth=auth,
		token_verifier=token_verifier,
	)

	def _handle_error(exc: Exception) -> str:
		if isinstance(exc, BrokerError):
			logger.warning("tool_error", code=exc.code, message=exc.message)
			raise exc
		logger.exception("internal_error")
		from broker.errors import InternalError

		raise InternalError(str(exc))

	@mcp.tool()
	async def session_create(
		workspace: str,
		model: str | None = None,
		effort: str | None = None,
		system_prompt_append: str | None = None,
		permission_policy: str | None = None,
		allowed_tools: list[str] | None = None,
		disallowed_tools: list[str] | None = None,
		max_budget_usd: float | None = None,
		max_turns: int | None = None,
		mcp_servers: dict[str, Any] | None = None,
		agents: dict[str, Any] | None = None,
		additional_directories: list[str] | None = None,
		setting_sources: list[str] | None = None,
		resume_from: str | None = None,
		fork_from: str | None = None,
		label: str | None = None,
		idle_ttl_seconds: int | None = None,
	) -> dict[str, Any]:
		"""Start a new Claude session."""
		try:
			args = SessionCreateArgs(
				workspace=workspace,
				model=model,
				effort=effort,
				system_prompt_append=system_prompt_append,
				permission_policy=permission_policy,
				allowed_tools=allowed_tools or [],
				disallowed_tools=disallowed_tools or [],
				max_budget_usd=max_budget_usd,
				max_turns=max_turns,
				mcp_servers=mcp_servers or {},
				agents=agents,
				additional_directories=additional_directories or [],
				setting_sources=setting_sources or [],
				resume_from=resume_from,
				fork_from=fork_from,
				label=label,
				idle_ttl_seconds=idle_ttl_seconds,
			)
			if args.resume_from and args.fork_from:
				raise InvalidArgumentError("resume_from and fork_from are mutually exclusive")
			return await core.create_session(
				args.workspace,
				model=args.model,
				effort=args.effort,
				system_prompt_append=args.system_prompt_append,
				permission_policy=args.permission_policy,
				allowed_tools=args.allowed_tools,
				disallowed_tools=args.disallowed_tools,
				max_budget_usd=args.max_budget_usd,
				max_turns=args.max_turns,
				mcp_servers=args.mcp_servers,
				agents=args.agents,
				additional_directories=args.additional_directories,
				setting_sources=args.setting_sources,
				resume_from=args.resume_from,
				fork_from=args.fork_from,
				label=args.label,
				idle_ttl_seconds=args.idle_ttl_seconds,
			)
		except Exception as exc:
			return _handle_error(exc)  # type: ignore[return-value]

	@mcp.tool()
	async def session_send(
		session_id: str,
		prompt: str,
		wait_ms: int = 0,
	) -> dict[str, Any]:
		"""Submit a prompt as a new turn."""
		try:
			args = SessionSendArgs(session_id=session_id, prompt=prompt, wait_ms=wait_ms)
			return await core.send(args.session_id, args.prompt, args.wait_ms)
		except Exception as exc:
			return _handle_error(exc)  # type: ignore[return-value]

	@mcp.tool()
	async def session_poll(
		session_id: str,
		cursor: int = 0,
		wait_ms: int = 0,
		limit: int = 200,
		include: list[str] | None = None,
	) -> dict[str, Any]:
		"""Poll session events from a cursor."""
		try:
			args = SessionPollArgs(
				session_id=session_id,
				cursor=cursor,
				wait_ms=wait_ms,
				limit=limit,
				include=include,
			)
			return await core.poll_async(
				args.session_id,
				cursor=args.cursor,
				wait_ms=args.wait_ms,
				limit=args.limit,
				include=args.include,
			)
		except Exception as exc:
			return _handle_error(exc)  # type: ignore[return-value]

	@mcp.tool()
	async def session_interrupt(session_id: str) -> dict[str, Any]:
		"""Cancel the in-flight turn."""
		try:
			args = SessionInterruptArgs(session_id=session_id)
			return await core.interrupt(args.session_id)
		except Exception as exc:
			return _handle_error(exc)  # type: ignore[return-value]

	@mcp.tool()
	async def session_fork(
		session_id: str,
		label: str | None = None,
		model: str | None = None,
		permission_policy: str | None = None,
		max_budget_usd: float | None = None,
		workspace: str | None = None,
	) -> dict[str, Any]:
		"""Branch a session at its current state."""
		try:
			args = SessionForkArgs(
				session_id=session_id,
				label=label,
				model=model,
				permission_policy=permission_policy,
				max_budget_usd=max_budget_usd,
				workspace=workspace,
			)
			return await core.fork_session(
				args.session_id,
				label=args.label,
				model=args.model,
				permission_policy=args.permission_policy,
				max_budget_usd=args.max_budget_usd,
				workspace=args.workspace,
			)
		except Exception as exc:
			return _handle_error(exc)  # type: ignore[return-value]

	@mcp.tool()
	async def session_close(
		session_id: str,
		reason: str | None = None,
	) -> dict[str, Any]:
		"""Terminate and release session resources."""
		try:
			args = SessionCloseArgs(session_id=session_id, reason=reason)
			return await core.close_session(args.session_id, reason=args.reason or "")
		except Exception as exc:
			return _handle_error(exc)  # type: ignore[return-value]

	@mcp.tool()
	async def session_list(
		state: list[str] | None = None,
		include_closed: bool = False,
		limit: int = 50,
	) -> dict[str, Any]:
		"""Enumerate active sessions."""
		try:
			args = SessionListArgs(state=state, include_closed=include_closed, limit=limit)
			return core.list_sessions(
				state=args.state,
				include_closed=args.include_closed,
				limit=args.limit,
			)
		except Exception as exc:
			return _handle_error(exc)  # type: ignore[return-value]

	@mcp.tool()
	async def session_transcript(
		session_id: str,
		format: str = "json",
		from_cursor: int = 0,
		to_cursor: int | None = None,
		include: list[str] | None = None,
	) -> dict[str, Any]:
		"""Full or filtered session history."""
		try:
			args = SessionTranscriptArgs(
				session_id=session_id,
				format=format,
				from_cursor=from_cursor,
				to_cursor=to_cursor,
				include=include,
			)
			return core.transcript(
				args.session_id,
				format=args.format,
				from_cursor=args.from_cursor,
				to_cursor=args.to_cursor,
				include=args.include,
			)
		except Exception as exc:
			return _handle_error(exc)  # type: ignore[return-value]

	@mcp.tool()
	async def permission_pending(session_id: str | None = None) -> dict[str, Any]:
		"""List parked permission requests."""
		try:
			args = PermissionPendingArgs(session_id=session_id)
			return {"pending": core.permissions.list_pending(args.session_id)}
		except Exception as exc:
			return _handle_error(exc)  # type: ignore[return-value]

	@mcp.tool()
	async def permission_resolve(
		request_id: str,
		decision: str,
		reason: str | None = None,
		updated_input: dict[str, Any] | None = None,
		interrupt: bool = False,
		remember: str = "none",
	) -> dict[str, Any]:
		"""Allow or deny a parked permission request."""
		try:
			args = PermissionResolveArgs(
				request_id=request_id,
				decision=decision,
				reason=reason,
				updated_input=updated_input,
				interrupt=interrupt,
				remember=remember,
			)
			if args.decision not in ("allow", "deny"):
				raise InvalidArgumentError("decision must be allow or deny")
			return core.permissions.resolve(
				args.request_id,
				args.decision,
				reason=args.reason,
				updated_input=args.updated_input,
				interrupt=args.interrupt,
				remember=args.remember,
			)
		except Exception as exc:
			return _handle_error(exc)  # type: ignore[return-value]

	@mcp.tool()
	async def broker_status() -> dict[str, Any]:
		"""Health, auth, budget, and rate-limit state."""
		return core.broker_status()

	@mcp.tool()
	async def workspace_list() -> dict[str, Any]:
		"""Enumerate mounted workspace roots."""
		return {"roots": list_roots(config.workspace_roots)}

	return mcp


def verify_bearer(auth_header: str | None, token: str | None) -> bool:
	if not token:
		return True
	if not auth_header or not auth_header.startswith("Bearer "):
		return False
	provided = auth_header[7:]
	return hmac.compare_digest(provided, token)
