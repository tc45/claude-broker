"""Cost ledger and rate-limit governor."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from broker.errors import AuthFailedError, BudgetExceededError, RateLimitedError
from broker.registry import SessionCost

logger = structlog.get_logger()


def utcnow() -> datetime:
	return datetime.now(tz=UTC)


def round_usd(value: float) -> float:
	return round(value, 6)


@dataclass
class LedgerRecord:
	at: str
	session_id: str
	turn_id: str
	model: str
	cost_usd: float
	usage: dict[str, Any]
	num_turns: int
	duration_ms: int
	stop_reason: str | None


class CostLedger:
	"""Global and per-session cost tracking with JSONL persistence."""

	def __init__(
		self,
		state_dir: Path,
		global_budget_usd: float | None = None,
		budget_window: str = "monthly",
	) -> None:
		self._state_dir = state_dir
		self._ledger_path = state_dir / "ledger.jsonl"
		self._global_budget_usd = global_budget_usd
		self._budget_window = budget_window
		self._global_spent = 0.0
		self._turns = 0
		self._by_model: dict[str, float] = {}
		self._window_started_at = self._window_start()
		self._state_dir.mkdir(parents=True, exist_ok=True)
		self.replay()

	def _window_start(self) -> datetime:
		now = utcnow()
		if self._budget_window == "monthly":
			return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
		return now.replace(hour=0, minute=0, second=0, microsecond=0)

	def replay(self) -> None:
		"""Replay ledger file to reconstruct global totals."""
		self._global_spent = 0.0
		self._turns = 0
		self._by_model = {}
		if not self._ledger_path.exists():
			return
		window_start = self._window_started_at
		with open(self._ledger_path, encoding="utf-8") as f:
			for line in f:
				line = line.strip()
				if not line:
					continue
				try:
					record = json.loads(line)
					at = datetime.fromisoformat(record["at"].replace("Z", "+00:00"))
					if at < window_start:
						continue
					cost = record.get("cost_usd", 0.0) or 0.0
					self._global_spent += cost
					self._turns += 1
					model = record.get("model", "unknown")
					self._by_model[model] = self._by_model.get(model, 0.0) + cost
				except (json.JSONDecodeError, KeyError, ValueError):
					continue

	def record_turn(
		self,
		session_id: str,
		turn_id: str,
		result_data: dict[str, Any],
		duration_ms: int = 0,
	) -> None:
		"""Record a completed turn from ResultMessage data."""
		cost = result_data.get("total_cost_usd")
		if cost is None:
			logger.warning("ResultMessage missing total_cost_usd, treating as 0")
			cost = 0.0
		cost = float(cost)
		usage = result_data.get("usage") or {}
		model_usage = result_data.get("model_usage") or {}
		model = "unknown"
		if model_usage:
			model = next(iter(model_usage.keys()), "unknown")
		record = {
			"at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
			"session_id": session_id,
			"turn_id": turn_id,
			"model": model,
			"cost_usd": round_usd(cost),
			"usage": usage,
			"num_turns": result_data.get("num_turns", 0),
			"duration_ms": duration_ms,
			"stop_reason": result_data.get("stop_reason"),
		}
		with open(self._ledger_path, "a", encoding="utf-8") as f:
			f.write(json.dumps(record) + "\n")
		self._global_spent += cost
		self._turns += 1
		self._by_model[model] = self._by_model.get(model, 0.0) + cost

	def update_session_cost(self, session_cost: SessionCost, result_data: dict[str, Any]) -> None:
		cost = result_data.get("total_cost_usd")
		if cost is None:
			cost = 0.0
		cost = float(cost)
		session_cost.total_usd += cost
		session_cost.turns += 1
		usage = result_data.get("usage") or {}
		session_cost.input_tokens += usage.get("input_tokens", 0)
		session_cost.output_tokens += usage.get("output_tokens", 0)
		session_cost.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
		session_cost.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
		model_usage = result_data.get("model_usage") or {}
		for model, info in model_usage.items():
			if isinstance(info, dict):
				model_cost = info.get("cost_usd", 0.0) or 0.0
			else:
				model_cost = float(info) if info else 0.0
			session_cost.by_model[model] = session_cost.by_model.get(model, 0.0) + float(model_cost)

	def check_global_budget(self) -> None:
		if self._global_budget_usd is not None and self._global_spent >= self._global_budget_usd:
			raise BudgetExceededError(
				f"Global budget of ${self._global_budget_usd:.2f} exceeded",
				details={
					"global_spent_usd": round_usd(self._global_spent),
					"global_limit_usd": self._global_budget_usd,
				},
			)

	def status(self) -> dict[str, Any]:
		return {
			"window": self._budget_window,
			"window_started_at": self._window_started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
			"global_spent_usd": round_usd(self._global_spent),
			"global_limit_usd": self._global_budget_usd,
			"turns": self._turns,
			"by_model": {k: round_usd(v) for k, v in self._by_model.items()},
		}


@dataclass
class RateLimitState:
	state: str = "ok"
	cooldown_until: datetime | None = None
	consecutive_rate_limits: int = 0
	recent_retries: int = 0
	auth_failed: bool = False
	auth_message: str | None = None


class RateLimitGovernor:
	"""Reactive rate-limit governor with global cooldown."""

	FAIL_FAST_ERRORS = frozenset({
		"authentication_failed",
		"oauth_org_not_allowed",
		"billing_error",
	})

	def __init__(self, cooldown_base: float = 30.0, cooldown_max: float = 900.0) -> None:
		self._cooldown_base = cooldown_base
		self._cooldown_max = cooldown_max
		self._state = RateLimitState()
		self._rng = random.Random()

	def handle_api_retry(self, data: dict[str, Any]) -> None:
		error = data.get("error", "unknown")
		self._state.recent_retries += 1

		if error in self.FAIL_FAST_ERRORS:
			self._state.auth_failed = True
			self._state.auth_message = f"Authentication/billing error: {error}"
			raise AuthFailedError(self._state.auth_message)

		if error == "rate_limit":
			self._state.consecutive_rate_limits += 1
			retry_delay_ms = data.get("retry_delay_ms", 0)
			base = max(
				retry_delay_ms / 1000,
				self._cooldown_base * (2 ** (self._state.consecutive_rate_limits - 1)),
			)
			cooldown_seconds = min(self._cooldown_max, base)
			jitter = self._rng.uniform(0, cooldown_seconds)
			actual = max(retry_delay_ms / 1000, jitter)
			from datetime import timedelta

			self._state.cooldown_until = utcnow() + timedelta(seconds=actual)
			self._state.state = "cooldown"
		elif error == "overloaded":
			from datetime import timedelta

			self._state.cooldown_until = utcnow() + timedelta(seconds=10)
			self._state.state = "cooldown"

	def on_turn_complete(self) -> None:
		self._state.consecutive_rate_limits = 0
		if self._state.cooldown_until and utcnow() >= self._state.cooldown_until:
			self._state.state = "ok"
			self._state.cooldown_until = None

	def is_in_cooldown(self) -> bool:
		if self._state.cooldown_until and utcnow() >= self._state.cooldown_until:
			self._state.state = "ok"
			self._state.cooldown_until = None
		return self._state.state == "cooldown" and self._state.cooldown_until is not None

	def retry_after_seconds(self) -> float:
		if not self._state.cooldown_until:
			return 0.0
		remaining = (self._state.cooldown_until - utcnow()).total_seconds()
		return max(0.0, remaining)

	def assert_sendable(self) -> None:
		if self._state.auth_failed:
			raise AuthFailedError(self._state.auth_message or "Authentication failed")
		if self.is_in_cooldown():
			raise RateLimitedError(self.retry_after_seconds())

	def status(self) -> dict[str, Any]:
		return {
			"state": self._state.state,
			"cooldown_until": (
				self._state.cooldown_until.strftime("%Y-%m-%dT%H:%M:%SZ")
				if self._state.cooldown_until
				else None
			),
			"recent_retries": self._state.recent_retries,
			"consecutive_rate_limits": self._state.consecutive_rate_limits,
		}

	def compute_cooldown_seconds(self, consecutive: int, retry_delay_ms: int) -> float:
		"""Compute cooldown with jitter for testing."""
		base = max(
			retry_delay_ms / 1000,
			self._cooldown_base * (2 ** (consecutive - 1)),
		)
		cooldown_seconds = min(self._cooldown_max, base)
		return self._rng.uniform(0, cooldown_seconds)
