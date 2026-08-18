"""Unit tests for config and preflight."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from broker.config import Config, ConfigError, preflight, version_at_least


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
	for var in (
		"ANTHROPIC_API_KEY",
		"ANTHROPIC_AUTH_TOKEN",
		"CLAUDE_CODE_USE_BEDROCK",
		"CLAUDE_CODE_USE_VERTEX",
		"CLAUDE_CODE_USE_FOUNDRY",
		"BROKER_ALLOW_API_BILLING",
		"BROKER_SKIP_AUTH_PROBE",
	):
		monkeypatch.delenv(var, raising=False)
	monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
	monkeypatch.setenv("BROKER_SKIP_AUTH_PROBE", "1")


def _base_config(**overrides: object) -> Config:
	defaults = dict(
		transport="http",
		host="127.0.0.1",
		port=8787,
		auth_token=None,
		oauth_token="test-token",
		workspace_roots=(Path("/workspace"),),
		state_dir=Path("/tmp/state"),
		default_policy="reviewed",
		policy_file=Path("policies/default.yaml"),
		max_sessions=8,
		session_idle_ttl=3600,
		session_retain=86400,
		event_memory_limit=5000,
		permission_timeout=300,
		global_budget_usd=None,
		default_budget_usd=2.0,
		allow_api_billing=False,
		cli_path=Path("/usr/local/bin/claude"),
		log_level="INFO",
		cooldown_base=30.0,
		cooldown_max=900.0,
		budget_window="monthly",
		passthrough_extra_args=(),
		billing_mode="subscription",
	)
	defaults.update(overrides)
	return Config(**defaults)


def test_u1_anthropic_api_key_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
	cfg = _base_config(state_dir=tmp_path, workspace_roots=(tmp_path,))
	tmp_path.mkdir(exist_ok=True)
	with patch("broker.config.probe_cli_version", return_value="2.1.219"):
		with patch("broker.config.probe_auth"):
			with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
				preflight(cfg)


def test_u2_anthropic_auth_token_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
	cfg = _base_config(state_dir=tmp_path, workspace_roots=(tmp_path,))
	with patch("broker.config.probe_cli_version", return_value="2.1.219"):
		with patch("broker.config.probe_auth"):
			with pytest.raises(ConfigError, match="ANTHROPIC_AUTH_TOKEN"):
				preflight(cfg)


@pytest.mark.parametrize(
	"var",
	["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"],
)
def test_u3_cloud_vars_rejected(var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv(var, "1")
	cfg = _base_config(state_dir=tmp_path, workspace_roots=(tmp_path,))
	with patch("broker.config.probe_cli_version", return_value="2.1.219"):
		with patch("broker.config.probe_auth"):
			with pytest.raises(ConfigError, match=var):
				preflight(cfg)


def test_u4_api_billing_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
	cfg = _base_config(
		state_dir=tmp_path,
		workspace_roots=(tmp_path,),
		allow_api_billing=True,
		billing_mode="api",
	)
	with patch("broker.config.probe_cli_version", return_value="2.1.219"):
		with patch("broker.config.probe_auth"):
			preflight(cfg)
	assert cfg.billing_mode == "api"


def test_u5_no_oauth_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
	cfg = _base_config(state_dir=tmp_path, workspace_roots=(tmp_path,), oauth_token=None)
	with patch("broker.config.probe_cli_version", return_value="2.1.219"):
		with pytest.raises(ConfigError, match="setup-token"):
			preflight(cfg)


def test_u6_cli_too_old(tmp_path: Path) -> None:
	cfg = _base_config(state_dir=tmp_path, workspace_roots=(tmp_path,))
	with patch("broker.config.probe_cli_version", return_value="2.1.218"):
		with patch("broker.config.probe_auth"):
			with pytest.raises(ConfigError, match="2.1.219"):
				preflight(cfg)


@pytest.mark.parametrize("version", ["2.1.219", "2.2.0"])
def test_u7_cli_ok(version: str, tmp_path: Path) -> None:
	cfg = _base_config(state_dir=tmp_path, workspace_roots=(tmp_path,))
	with patch("broker.config.probe_cli_version", return_value=version):
		with patch("broker.config.probe_auth"):
			preflight(cfg)


def test_u8_bare_rejected(tmp_path: Path) -> None:
	cfg = _base_config(
		state_dir=tmp_path,
		workspace_roots=(tmp_path,),
		passthrough_extra_args=("bare",),
	)
	with patch("broker.config.probe_cli_version", return_value="2.1.219"):
		with pytest.raises(ConfigError, match="bare"):
			preflight(cfg)


def test_u9_http_no_auth_non_loopback(tmp_path: Path) -> None:
	cfg = _base_config(
		state_dir=tmp_path,
		workspace_roots=(tmp_path,),
		host="0.0.0.0",
		auth_token=None,
	)
	with patch("broker.config.probe_cli_version", return_value="2.1.219"):
		with patch("broker.config.probe_auth"):
			with pytest.raises(ConfigError, match="BROKER_AUTH_TOKEN"):
				preflight(cfg)


def test_u10_loopback_no_auth_ok(tmp_path: Path) -> None:
	cfg = _base_config(
		state_dir=tmp_path,
		workspace_roots=(tmp_path,),
		host="127.0.0.1",
		auth_token=None,
	)
	with patch("broker.config.probe_cli_version", return_value="2.1.219"):
		with patch("broker.config.probe_auth"):
			preflight(cfg)


def test_version_at_least() -> None:
	assert version_at_least("2.1.219", "2.1.219")
	assert version_at_least("2.2.0", "2.1.219")
	assert not version_at_least("2.1.218", "2.1.219")


def test_probe_cli_version_parses_suffix() -> None:
	from unittest.mock import MagicMock, patch

	from broker.config import probe_cli_version

	result = MagicMock(returncode=0, stdout="2.1.219 (Claude Code)\n", stderr="")
	with patch("broker.config.subprocess.run", return_value=result):
		assert probe_cli_version(Path("/usr/bin/claude")) == "2.1.219"
