"""The event log has to be worth reading.

58% of the events in a real session were type=error / UNMAPPED_MESSAGE. Two of
those categories were pure telemetry and one was the single most useful thing in
the log — tool results — reaching the unmapped branch because `normalise` only
unpacked blocks for AssistantMessage.
"""

from __future__ import annotations

from types import SimpleNamespace

from broker.normalise import normalise


class ToolResultBlock(SimpleNamespace):
	pass


class TextBlock(SimpleNamespace):
	pass


class ThinkingBlock(SimpleNamespace):
	pass


class UserMessage(SimpleNamespace):
	pass


class AssistantMessage(SimpleNamespace):
	pass


class SystemMessage(SimpleNamespace):
	pass


class RateLimitEvent(SimpleNamespace):
	pass


def test_tool_results_on_a_user_message_are_mapped() -> None:
	msg = UserMessage(content=[
		ToolResultBlock(tool_use_id="tu_1", is_error=False, content="hello from the tool")
	])
	events = normalise(msg, 0)
	assert [e.type for e in events] == ["tool_result"]
	assert events[0].data["tool_use_id"] == "tu_1"
	assert "hello from the tool" in events[0].data["content"]


def test_thinking_token_telemetry_is_dropped() -> None:
	msg = SystemMessage(subtype="thinking_tokens", data={"type": "budget", "n": 1024})
	assert normalise(msg, 0) == []


def test_an_unknown_system_subtype_is_visible_but_not_an_error() -> None:
	"""Dropping everything unknown would hide a real signal; calling it an error
	is what trained the operator to ignore the error colour."""
	msg = SystemMessage(subtype="something_new", data={"a": 1})
	events = normalise(msg, 0)
	assert [e.type for e in events] == ["system"]
	assert events[0].data["subtype"] == "something_new"


def test_empty_thinking_blocks_are_dropped() -> None:
	msg = AssistantMessage(content=[
		ThinkingBlock(thinking="  "),
		TextBlock(text="the actual answer"),
	])
	events = normalise(msg, 0)
	assert [e.type for e in events] == ["assistant_text"]


def test_rate_limit_carries_the_reset_time() -> None:
	"""The regression: this read msg.status/msg.retry_after, which do not exist,
	so a real rate limit logged as "None retry after Nones" and the pipeline
	stopped with nothing to say. resets_at is what makes resuming possible."""
	info = SimpleNamespace(
		status="rejected", resets_at=1787100000, rate_limit_type="five_hour",
		utilization=1.0, overage_status=None, overage_resets_at=None,
		overage_disabled_reason=None, raw={},
	)
	events = normalise(RateLimitEvent(rate_limit_info=info), 0)
	assert [e.type for e in events] == ["rate_limit"]
	assert events[0].data["status"] == "rejected"
	assert events[0].data["resets_at"] == 1787100000


def test_task_progress_is_a_subagent_event() -> None:
	class TaskProgressMessage(SimpleNamespace):
		pass

	events = normalise(
		TaskProgressMessage(subtype="task_progress", data={"task_id": "t1"}), 0
	)
	assert [e.type for e in events] == ["subagent"]


def test_a_plain_string_user_turn_adds_nothing() -> None:
	"""The prompt MOM just sent is not news to the log."""
	assert normalise(UserMessage(content="do the thing"), 0) == []


class TaskUpdatedMessage(SimpleNamespace):
	pass


def test_subagent_progress_is_readable() -> None:
	"""Fan-out made this the line that matters: which worker is still going."""
	msg = TaskUpdatedMessage(
		subtype="task_updated",
		data={"task_id": "a03", "patch": {"status": "completed", "title": "Prior art",
										   "tool_use_count": 7}},
	)
	events = normalise(msg, 0)
	assert [e.type for e in events] == ["subagent"]
	assert events[0].data["status"] == "completed"
	assert events[0].data["title"] == "Prior art"
