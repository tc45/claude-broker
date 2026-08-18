"""Broker entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

from broker.config import Config, load_config, preflight
from broker.core import BrokerCore
from broker.mcp_server import create_mcp_server
from broker.reaper import Reaper


def configure_logging(level: str) -> None:
	structlog.configure(
		processors=[
			structlog.processors.add_log_level,
			structlog.processors.TimeStamper(fmt="iso"),
			structlog.dev.ConsoleRenderer(),
		],
		wrapper_class=structlog.make_filtering_bound_logger(
			getattr(structlog, "INFO", 20)
			if level == "INFO"
			else 10
		),
	)


def main() -> None:
	parser = argparse.ArgumentParser(description="Claude broker MCP server")
	parser.parse_args()

	try:
		cfg = load_config()
		preflight(cfg)
	except Exception as exc:
		print(f"Preflight failed: {exc}", file=sys.stderr)
		sys.exit(1)

	configure_logging(cfg.log_level)
	logger = structlog.get_logger()
	logger.info("Starting claude-broker", transport=cfg.transport, host=cfg.host, port=cfg.port)

	core = BrokerCore(cfg)
	reaper = Reaper(core)

	if cfg.transport == "stdio":
		asyncio.run(_run_stdio(core, cfg, reaper))
	else:
		asyncio.run(_run_http(core, cfg, reaper))


async def _run_stdio(core: BrokerCore, cfg: Config, reaper: Reaper) -> None:
	reaper.start()
	try:
		mcp = create_mcp_server(core, cfg)
		await mcp.run_stdio_async()
	finally:
		reaper.stop()


async def _run_http(core: BrokerCore, cfg: Config, reaper: Reaper) -> None:
	reaper.start()
	try:
		mcp = create_mcp_server(core, cfg)
		await mcp.run_streamable_http_async()
	finally:
		reaper.stop()


if __name__ == "__main__":
	main()
