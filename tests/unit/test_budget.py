"""Unit tests for cost ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from broker.budget import CostLedger
from broker.errors import BudgetExceededError
from broker.registry import SessionCost


def test_u37_accumulate(tmp_path: Path) -> None:
	ledger = CostLedger(tmp_path, global_budget_usd=100.0)
	ledger.record_turn("s1", "t1", {"total_cost_usd": 0.1, "usage": {}, "num_turns": 1})
	ledger.record_turn("s1", "t2", {"total_cost_usd": 0.2, "usage": {}, "num_turns": 1})
	assert ledger._global_spent == pytest.approx(0.3)


def test_u38_none_cost(tmp_path: Path) -> None:
	ledger = CostLedger(tmp_path)
	ledger.record_turn("s1", "t1", {"total_cost_usd": None, "usage": {}, "num_turns": 1})
	assert ledger._global_spent == 0.0


def test_u39_global_budget_exceeded(tmp_path: Path) -> None:
	ledger = CostLedger(tmp_path, global_budget_usd=1.0)
	ledger.record_turn("s1", "t1", {"total_cost_usd": 1.5, "usage": {}, "num_turns": 1})
	with pytest.raises(BudgetExceededError):
		ledger.check_global_budget()


def test_u40_replay(tmp_path: Path) -> None:
	ledger = CostLedger(tmp_path, global_budget_usd=100.0)
	ledger.record_turn("s1", "t1", {"total_cost_usd": 0.5, "usage": {}, "num_turns": 1})
	ledger2 = CostLedger(tmp_path, global_budget_usd=100.0)
	assert ledger2._global_spent == pytest.approx(0.5)


def test_u41_monthly_window(tmp_path: Path) -> None:
	ledger_path = tmp_path / "ledger.jsonl"
	old = (datetime.now(tz=UTC) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
	with open(ledger_path, "w") as f:
		f.write(json.dumps({"at": old, "cost_usd": 99.0, "model": "x"}) + "\n")
	now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
	with open(ledger_path, "a") as f:
		f.write(json.dumps({"at": now, "cost_usd": 1.0, "model": "x"}) + "\n")
	ledger = CostLedger(tmp_path, global_budget_usd=100.0)
	assert ledger._global_spent == pytest.approx(1.0)


def test_u42_by_model(tmp_path: Path) -> None:
	cost = SessionCost()
	ledger = CostLedger(tmp_path)
	ledger.update_session_cost(cost, {
		"total_cost_usd": 0.5,
		"model_usage": {"claude-opus-5": {"cost_usd": 0.5}},
		"usage": {"input_tokens": 100, "output_tokens": 50},
	})
	assert cost.by_model.get("claude-opus-5") == pytest.approx(0.5)
