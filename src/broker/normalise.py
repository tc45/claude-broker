"""SDK message to broker event normalisation."""

from __future__ import annotations

import json
from typing import Any

from broker.event_log import Event, format_ts, utcnow

TOOL_RESULT_MAX = 4096

#: SystemMessage subtypes that are per-turn telemetry, not events. Dropped
#: rather than downgraded: a log line whose only content is a token counter
#: costs an operator attention on every scroll and repays none of it.
QUIET_SYSTEM_SUBTYPES = frozenset({
	"thinking_tokens",
	"mirror_error",  # surfaced through the store's own error path already
})


def _safe_repr(obj: Any) -> str:
	try:
		return repr(obj)[:4096]
	except Exception:
		return "<unreprable>"


def _is_empty(event: Event) -> bool:
	"""An event carrying no information. A ThinkingBlock with empty text is the
	common one — the SDK emits it whenever extended thinking is on but produced
	nothing for that block, and it reads in the log as if something happened."""
	if event.type == "thinking":
		return not (event.data.get("text") or "").strip()
	if event.type == "assistant_text":
		return not (event.data.get("text") or "").strip()
	return False


def _block_event(block: Any, idx: int) -> Event:
	block_type = type(block).__name__
	if block_type == "TextBlock":
		return Event(
			index=idx,
			type="assistant_text",
			at=format_ts(utcnow()),
			data={"text": getattr(block, "text", str(block))},
		)
	if block_type == "ThinkingBlock":
		return Event(
			index=idx,
			type="thinking",
			at=format_ts(utcnow()),
			data={"text": getattr(block, "thinking", getattr(block, "text", ""))},
		)
	if block_type == "ToolUseBlock":
		return Event(
			index=idx,
			type="tool_use",
			at=format_ts(utcnow()),
			data={
				"tool": getattr(block, "name", ""),
				"tool_use_id": getattr(block, "id", ""),
				"input": getattr(block, "input", {}),
				"parent_tool_use_id": getattr(block, "parent_tool_use_id", None),
			},
		)
	if block_type == "ToolResultBlock":
		content = getattr(block, "content", "")
		content_str = content if isinstance(content, str) else json.dumps(content, default=str)
		truncated = len(content_str) > TOOL_RESULT_MAX
		if truncated:
			content_str = content_str[:TOOL_RESULT_MAX]
		summary = content_str[:120] if content_str else ""
		return Event(
			index=idx,
			type="tool_result",
			at=format_ts(utcnow()),
			data={
				"tool_use_id": getattr(block, "tool_use_id", ""),
				"is_error": getattr(block, "is_error", False),
				"summary": summary,
				"content": content_str,
				"truncated": truncated,
			},
		)
	return Event(
		index=idx,
		type="error",
		at=format_ts(utcnow()),
		data={"code": "UNMAPPED_BLOCK", "message": block_type, "details": _safe_repr(block)},
	)


def normalise(msg: Any, idx: int) -> list[Event]:
	"""Map an SDK message to one or more broker events."""
	msg_type = type(msg).__name__

	if msg_type == "SystemMessage":
		subtype = getattr(msg, "subtype", None)
		data = getattr(msg, "data", {}) or {}
		if subtype == "init":
			return [
				Event(
					index=idx,
					type="session_init",
					at=format_ts(utcnow()),
					data={
						"model": data.get("model"),
						"tools": data.get("tools", []),
						"mcp_servers": data.get("mcp_servers", []),
						"mcp_server_errors": data.get("mcp_server_errors", []),
						"capabilities": data.get("capabilities", []),
					},
				)
			]
		if subtype == "api_retry":
			return [
				Event(
					index=idx,
					type="api_retry",
					at=format_ts(utcnow()),
					data={
						"attempt": data.get("attempt", 0),
						"max_retries": data.get("max_retries", 0),
						"retry_delay_ms": data.get("retry_delay_ms", 0),
						"error": data.get("error", "unknown"),
						"error_status": data.get("error_status"),
					},
				)
			]

		# Everything else the CLI sends as a SystemMessage is telemetry about the
		# turn rather than an event in it. `thinking_tokens` alone was most of a
		# real session's log, every one of them reported as an *error* — which
		# teaches an operator that the error colour means nothing, and that is
		# how a genuine failure ends up unread.
		if subtype in QUIET_SYSTEM_SUBTYPES:
			return []
		return [
			Event(
				index=idx,
				type="system",
				at=format_ts(utcnow()),
				data={"subtype": subtype, "data": data},
			)
		]

	# Tool results arrive on a UserMessage — the SDK models them as the user
	# handing output back to the model. Without this branch the ToolResultBlock
	# mapping above was unreachable, so every tool result was an "unmapped"
	# Python repr: the most useful line in an ops log rendered as the least
	# readable thing in it.
	if msg_type in ("AssistantMessage", "UserMessage"):
		blocks = getattr(msg, "content", []) or []
		if isinstance(blocks, str):  # a plain text turn, nothing to unpack
			return []
		events = [_block_event(b, idx + i) for i, b in enumerate(blocks)]
		return [e for e in events if not _is_empty(e)]

	if msg_type == "RateLimitEvent":
		return [
			Event(
				index=idx,
				type="rate_limit",
				at=format_ts(utcnow()),
				data={
					"status": getattr(msg, "status", None),
					"retry_after": getattr(msg, "retry_after", None),
					"details": _safe_repr(msg)[:500],
				},
			)
		]

	if msg_type == "ResultMessage":
		return [
			Event(
				index=idx,
				type="turn_result",
				at=format_ts(utcnow()),
				data={
					"is_error": getattr(msg, "is_error", False),
					"num_turns": getattr(msg, "num_turns", 0),
					"total_cost_usd": getattr(msg, "total_cost_usd", 0.0),
					"usage": getattr(msg, "usage", {}),
					"model_usage": getattr(msg, "model_usage", {}),
					"result": getattr(msg, "result", None),
					"stop_reason": getattr(msg, "stop_reason", None),
					"permission_denials": getattr(msg, "permission_denials", []),
					"errors": getattr(msg, "errors", []),
					"terminal_reason": getattr(msg, "terminal_reason", None),
				},
			)
		]

	return [
		Event(
			index=idx,
			type="error",
			at=format_ts(utcnow()),
			data={
				"code": "UNMAPPED_MESSAGE",
				"message": msg_type,
				"details": _safe_repr(msg),
			},
		)
	]
