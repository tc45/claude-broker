"""Live tests requiring real CLAUDE_CODE_OAUTH_TOKEN."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live


@pytest.fixture
def live_token() -> str:
	token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
	if not token:
		pytest.skip("CLAUDE_CODE_OAUTH_TOKEN not set")
	return token


@pytest.mark.asyncio
async def test_l1_preflight_auth(live_token: str) -> None:
	from broker.config import Config, preflight

	os.environ["BROKER_SKIP_AUTH_PROBE"] = "0"
	cfg = Config.from_env()
	with pytest.raises(Exception):
		preflight(cfg)


@pytest.mark.asyncio
async def test_l10_token_expiry(live_token: str) -> None:
	from broker.config import decode_token_expiry

	info = decode_token_expiry(live_token)
	assert info.get("days_remaining") is not None or info.get("token_expires_at") is None
