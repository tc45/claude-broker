"""SDK message to broker event normalisation."""

from __future__ import annotations

import json
from typing import Any

from broker.event_log import Event, format_ts, utcnow

TOOL_RESULT_MAX = 4096


def _safe_repr(obj: Any) -> str:
	try:
		return repr(obj)[:4096]
	except Exception:
		return "<unreprable>"


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

	if msg_type == "AssistantMessage":
		blocks = getattr(msg, "content", []) or []
		return [_block_event(b, idx + i) for i, b in enumerate(blocks)]

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
