"""Background session reaper."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from broker.registry import SessionState

if TYPE_CHECKING:
	from broker.core import BrokerCore

logger = structlog.get_logger()


class Reaper:
	"""Periodically reaps idle and terminal sessions."""

	def __init__(self, core: BrokerCore, interval: float = 60.0) -> None:
		self._core = core
		self._interval = interval
		self._task: asyncio.Task[None] | None = None

	def start(self) -> None:
		self._task = asyncio.create_task(self._run())

	def stop(self) -> None:
		if self._task:
			self._task.cancel()

	async def _run(self) -> None:
		while True:
			try:
				await asyncio.sleep(self._interval)
				await self._sweep()
			except asyncio.CancelledError:
				raise
			except Exception:
				logger.exception("Reaper sweep failed")

	async def _sweep(self) -> None:
		cfg = self._core.config
		now = datetime.now(tz=UTC)
		idle_ttl = timedelta(seconds=cfg.session_idle_ttl)
		retain = timedelta(seconds=cfg.session_retain)

		self._core.permissions.sweep_expired()

		for session in list(self._core.registry.list_all()):
			try:
				acquired = await asyncio.wait_for(session.lock.acquire(), timeout=5.0)
			except TimeoutError:
				continue
			if not acquired:
				continue
			try:
				if session.state == SessionState.IDLE:
					if now - session.last_active_at > idle_ttl:
						await self._core.close_session(session.session_id, reason="idle ttl")
				elif session.state.is_terminal:
					if now - session.last_active_at > retain:
						self._core.registry.remove(session.session_id)
			finally:
				session.lock.release()
