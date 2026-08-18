"""Environment configuration and startup preflight."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog

from broker.errors import ConfigError

logger = structlog.get_logger()

HIJACKER_VARS = (
	"ANTHROPIC_API_KEY",
	"ANTHROPIC_AUTH_TOKEN",
	"CLAUDE_CODE_USE_BEDROCK",
	"CLAUDE_CODE_USE_VERTEX",
	"CLAUDE_CODE_USE_FOUNDRY",
)

MIN_CLI_VERSION = "2.1.219"
_CLI_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


@dataclass(frozen=True)
class Config:
	transport: str
	host: str
	port: int
	auth_token: str | None
	oauth_token: str | None
	workspace_roots: tuple[Path, ...]
	state_dir: Path
	default_policy: str
	policy_file: Path
	max_sessions: int
	session_idle_ttl: int
	session_retain: int
	event_memory_limit: int
	permission_timeout: int
	global_budget_usd: float | None
	default_budget_usd: float
	allow_api_billing: bool
	cli_path: Path
	log_level: str
	cooldown_base: float
	cooldown_max: float
	budget_window: str
	passthrough_extra_args: tuple[str, ...]
	billing_mode: str = "subscription"
	# Use a mounted `claude /login` credential store (auth precedence #6) instead
	# of a minted CLAUDE_CODE_OAUTH_TOKEN. Defaults off so existing callers and
	# fixtures are unaffected.
	use_host_credentials: bool = False

	@classmethod
	def from_env(cls) -> Config:
		"""Load configuration from environment variables."""
		roots_raw = os.environ.get("BROKER_WORKSPACE_ROOTS", "/workspace")
		roots = tuple(Path(r) for r in roots_raw.split(":") if r)
		extra_args_raw = os.environ.get("BROKER_EXTRA_ARGS", "")
		extra_args = tuple(a for a in extra_args_raw.split() if a)
		global_budget = os.environ.get("BROKER_GLOBAL_BUDGET_USD")
		allow_api = os.environ.get("BROKER_ALLOW_API_BILLING", "0") in ("1", "true", "True")
		hijackers = [v for v in HIJACKER_VARS if os.environ.get(v)]
		if hijackers and not allow_api:
			billing_mode = "subscription"
		elif hijackers and allow_api:
			billing_mode = "api"
		else:
			billing_mode = "subscription"
		return cls(
			transport=os.environ.get("BROKER_TRANSPORT", "http"),
			host=os.environ.get("BROKER_HOST", "0.0.0.0"),
			port=int(os.environ.get("BROKER_PORT", "8787")),
			auth_token=os.environ.get("BROKER_AUTH_TOKEN"),
			oauth_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
			use_host_credentials=os.environ.get("BROKER_USE_HOST_CREDENTIALS", "0")
			in ("1", "true", "True"),
			workspace_roots=roots,
			state_dir=Path(os.environ.get("BROKER_STATE_DIR", "/var/lib/claude-broker")),
			default_policy=os.environ.get("BROKER_DEFAULT_POLICY", "reviewed"),
			policy_file=Path(os.environ.get("BROKER_POLICY_FILE", "policies/default.yaml")),
			max_sessions=int(os.environ.get("BROKER_MAX_SESSIONS", "8")),
			session_idle_ttl=int(os.environ.get("BROKER_SESSION_IDLE_TTL", "3600")),
			session_retain=int(os.environ.get("BROKER_SESSION_RETAIN", "86400")),
			event_memory_limit=int(os.environ.get("BROKER_EVENT_MEMORY_LIMIT", "5000")),
			permission_timeout=int(os.environ.get("BROKER_PERMISSION_TIMEOUT", "300")),
			global_budget_usd=float(global_budget) if global_budget else None,
			default_budget_usd=float(os.environ.get("BROKER_DEFAULT_BUDGET_USD", "2.00")),
			allow_api_billing=allow_api,
			cli_path=Path(os.environ.get("BROKER_CLI_PATH", "/usr/local/bin/claude")),
			log_level=os.environ.get("BROKER_LOG_LEVEL", "INFO"),
			cooldown_base=float(os.environ.get("BROKER_COOLDOWN_BASE", "30")),
			cooldown_max=float(os.environ.get("BROKER_COOLDOWN_MAX", "900")),
			budget_window=os.environ.get("BROKER_BUDGET_WINDOW", "monthly"),
			passthrough_extra_args=extra_args,
			billing_mode=billing_mode,
		)


def probe_cli_version(cli_path: Path) -> str:
	"""Return CLI version string from --version probe."""
	try:
		result = subprocess.run(
			[str(cli_path), "--version"],
			capture_output=True,
			text=True,
			timeout=30,
			check=False,
		)
		output = (result.stdout or result.stderr).strip()
		if result.returncode != 0:
			raise ConfigError(f"Cannot probe CLI at {cli_path}: {output}")
		match = _CLI_VERSION_RE.search(output)
		if not match:
			raise ConfigError(f"Cannot parse CLI version from {cli_path}: {output!r}")
		return match.group(1)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise ConfigError(f"Cannot probe CLI at {cli_path}: {exc}") from exc


def _parse_version(version: str) -> tuple[int, ...]:
	parts: list[int] = []
	for part in version.split("."):
		try:
			parts.append(int(part.split("-")[0]))
		except ValueError:
			break
	while len(parts) < 3:
		parts.append(0)
	return tuple(parts[:3])


def version_at_least(version: str, minimum: str) -> bool:
	return _parse_version(version) >= _parse_version(minimum)


def assert_writable(path: Path) -> None:
	try:
		path.mkdir(parents=True, exist_ok=True)
		test_file = path / ".write_test"
		test_file.write_text("ok")
		test_file.unlink()
	except OSError as exc:
		raise ConfigError(f"State directory {path} is not writable: {exc}") from exc


def assert_directory(path: Path) -> None:
	if not path.exists():
		raise ConfigError(f"Workspace root {path} does not exist")
	if not path.is_dir():
		raise ConfigError(f"Workspace root {path} is not a directory")


def decode_token_expiry(token: str | None) -> dict[str, Any]:
	"""Decode OAuth token expiry from JWT claims if possible."""
	if not token:
		return {"token_expires_at": None, "days_remaining": None, "warning": None}
	try:
		import base64
		import json
		from datetime import UTC, datetime

		parts = token.split(".")
		if len(parts) < 2:
			return {"token_expires_at": None, "days_remaining": None, "warning": None}
		payload = parts[1]
		padding = 4 - len(payload) % 4
		if padding != 4:
			payload += "=" * padding
		data = json.loads(base64.urlsafe_b64decode(payload))
		exp = data.get("exp")
		if not exp:
			return {"token_expires_at": None, "days_remaining": None, "warning": None}
		expires = datetime.fromtimestamp(exp, tz=UTC)
		now = datetime.now(tz=UTC)
		days = (expires - now).days
		warning = None
		if days < 30:
			warning = f"OAuth token expires in {days} days"
		return {
			"token_expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
			"days_remaining": days,
			"warning": warning,
		}
	except Exception:
		return {"token_expires_at": None, "days_remaining": None, "warning": None}


_auth_probe_result: dict[str, Any] | None = None


def set_auth_probe_result(result: dict[str, Any] | None) -> None:
	global _auth_probe_result
	_auth_probe_result = result


def get_auth_probe_result() -> dict[str, Any] | None:
	return _auth_probe_result


def probe_auth(cfg: Config) -> None:
	"""Run a trivial auth probe via the CLI."""
	if cfg.allow_api_billing and not cfg.oauth_token:
		set_auth_probe_result({"billing": "api", "method": "ANTHROPIC_API_KEY"})
		return
	if cfg.use_host_credentials:
		# A mounted `claude /login` store (auth precedence #6). It self-refreshes,
		# so there is no minted-token expiry to report.
		set_auth_probe_result({
			"method": "login-credentials",
			"billing": "subscription",
			"token_expires_at": None,
			"days_remaining": None,
			"warning": None,
		})
		return
	if not cfg.oauth_token:
		return
	# In tests, skip actual probe unless live
	if os.environ.get("BROKER_SKIP_AUTH_PROBE") == "1":
		set_auth_probe_result({
			"method": "CLAUDE_CODE_OAUTH_TOKEN",
			"billing": cfg.billing_mode,
			**decode_token_expiry(cfg.oauth_token),
		})
		return
	try:
		env = os.environ.copy()
		result = subprocess.run(
			[str(cfg.cli_path), "-p", "ok", "--output-format", "json", "--max-turns", "1"],
			capture_output=True,
			text=True,
			timeout=120,
			check=False,
			env=env,
			cwd=str(cfg.workspace_roots[0]) if cfg.workspace_roots else ".",
		)
		if result.returncode != 0:
			raise ConfigError(f"Auth probe failed: {result.stderr or result.stdout}")
		set_auth_probe_result({
			"method": "CLAUDE_CODE_OAUTH_TOKEN",
			"billing": "subscription",
			**decode_token_expiry(cfg.oauth_token),
		})
	except subprocess.TimeoutExpired as exc:
		raise ConfigError("Auth probe timed out") from exc
	except OSError as exc:
		raise ConfigError(f"Auth probe failed: {exc}") from exc


def preflight(cfg: Config) -> None:
	"""Run all startup preflight checks."""
	hijackers = [v for v in HIJACKER_VARS if os.environ.get(v)]
	if hijackers and not cfg.allow_api_billing:
		raise ConfigError(
			f"{', '.join(hijackers)} set and would take precedence over "
			f"CLAUDE_CODE_OAUTH_TOKEN, silently switching to API billing. "
			f"Unset it, or set BROKER_ALLOW_API_BILLING=1 to accept API charges."
		)
	if hijackers and cfg.allow_api_billing:
		logger.warning("API billing mode active", hijackers=hijackers)

	if not cfg.oauth_token and not cfg.allow_api_billing and not cfg.use_host_credentials:
		raise ConfigError(
			"CLAUDE_CODE_OAUTH_TOKEN is required. Run `claude setup-token` on a "
			"machine with a browser and pass the result in, or set "
			"BROKER_USE_HOST_CREDENTIALS=1 and mount an existing `claude /login` "
			"credential store at CLAUDE_CONFIG_DIR."
		)

	if cfg.use_host_credentials:
		if cfg.oauth_token:
			raise ConfigError(
				"BROKER_USE_HOST_CREDENTIALS=1 and CLAUDE_CODE_OAUTH_TOKEN are mutually "
				"exclusive: the token takes precedence over the mounted credential "
				"store, so the mount would be silently ignored. Unset one."
			)
		creds = Path(
			os.environ.get("CLAUDE_CONFIG_DIR", "/var/lib/claude-broker/claude")
		) / ".credentials.json"
		if not creds.is_file():
			raise ConfigError(
				f"BROKER_USE_HOST_CREDENTIALS=1 but no credential store at {creds}. "
				f"Copy a logged-in host's ~/.claude/.credentials.json to that path."
			)

	version = probe_cli_version(cfg.cli_path)
	if not version_at_least(version, MIN_CLI_VERSION):
		raise ConfigError(
			f"claude CLI {version} < required {MIN_CLI_VERSION} (see ADR-002)."
		)

	if "bare" in cfg.passthrough_extra_args:
		raise ConfigError(
			"--bare disables OAuth token reading and is incompatible with "
			"subscription auth. Use setting_sources=[] for isolation instead."
		)

	assert_writable(cfg.state_dir)
	for root in cfg.workspace_roots:
		assert_directory(root)

	if (
		cfg.transport == "http"
		and cfg.host not in ("127.0.0.1", "localhost")
		and not cfg.auth_token
	):
		raise ConfigError(
			"BROKER_AUTH_TOKEN is required when binding to a non-loopback address."
		)

	probe_auth(cfg)


@lru_cache(maxsize=1)
def load_config() -> Config:
	return Config.from_env()
