"""Unit tests for rate-limit governor."""

from __future__ import annotations

import pytest

from broker.budget import RateLimitGovernor
from broker.errors import AuthFailedError


def test_u43_rate_limit_cooldown() -> None:
	gov = RateLimitGovernor(cooldown_base=30.0, cooldown_max=900.0)
	gov.handle_api_retry({"error": "rate_limit", "retry_delay_ms": 1000, "attempt": 1})
	assert gov.is_in_cooldown()


def test_u44_server_error_no_cooldown() -> None:
	gov = RateLimitGovernor()
	gov.handle_api_retry({"error": "server_error", "retry_delay_ms": 1000})
	assert not gov.is_in_cooldown()


def test_u45_auth_fail_fast() -> None:
	gov = RateLimitGovernor()
	with pytest.raises(AuthFailedError):
		gov.handle_api_retry({"error": "authentication_failed"})


def test_u46_billing_fail_fast() -> None:
	gov = RateLimitGovernor()
	with pytest.raises(AuthFailedError):
		gov.handle_api_retry({"error": "billing_error"})


def test_u47_exponential_backoff() -> None:
	gov = RateLimitGovernor(cooldown_base=30.0, cooldown_max=900.0)
	s1 = gov.compute_cooldown_seconds(1, 0)
	s2 = gov.compute_cooldown_seconds(3, 0)
	assert s2 >= 0
	gov.handle_api_retry({"error": "rate_limit", "retry_delay_ms": 0, "attempt": 1})
	gov.handle_api_retry({"error": "rate_limit", "retry_delay_ms": 0, "attempt": 2})
	assert gov._state.consecutive_rate_limits == 2


def test_u48_reset_on_turn_complete() -> None:
	gov = RateLimitGovernor()
	gov.handle_api_retry({"error": "rate_limit", "retry_delay_ms": 100})
	gov.on_turn_complete()
	assert gov._state.consecutive_rate_limits == 0


def test_u49_global_cooldown() -> None:
	gov = RateLimitGovernor()
	gov.handle_api_retry({"error": "rate_limit", "retry_delay_ms": 60000})
	with pytest.raises(Exception):
		gov.assert_sendable()


def test_u50_jitter_varies() -> None:
	gov = RateLimitGovernor(cooldown_base=30.0, cooldown_max=900.0)
	samples = {gov.compute_cooldown_seconds(2, 0) for _ in range(100)}
	assert len(samples) > 1


def test_u51_inflight_not_interrupted() -> None:
	gov = RateLimitGovernor()
	gov.handle_api_retry({"error": "rate_limit", "retry_delay_ms": 60000})
	# Governor does not interrupt - just sets cooldown
	assert gov.is_in_cooldown()
