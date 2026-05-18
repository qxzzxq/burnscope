"""Command-line entry point for the BurnScope client daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from . import discovery
from .credentials import load_claude_token
from .daemon import DaemonConfig, run

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="burnscope-client",
        description="Tail Claude Code sessions and push usage snapshots to an ESP32.",
    )
    p.add_argument(
        "--esp32-host",
        help="Override mDNS discovery; format host or host:port (default port 80).",
    )
    p.add_argument(
        "--active-interval",
        type=float,
        default=60.0,
        help="Probe interval in seconds while recent agent activity is detected.",
    )
    p.add_argument(
        "--idle-interval",
        type=float,
        default=300.0,
        help="Probe interval in seconds when no recent activity.",
    )
    p.add_argument(
        "--active-window",
        type=float,
        default=300.0,
        help="How long (seconds) after a JSONL change we keep using active interval.",
    )
    p.add_argument(
        "--claude-projects-dir",
        type=Path,
        default=DEFAULT_PROJECTS_DIR,
        help="Directory to watch for Claude Code session JSONL files.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token = load_claude_token()
    host = discovery.parse_host_override(args.esp32_host) if args.esp32_host else None
    config = DaemonConfig(
        token=token,
        esp32_host=host,
        claude_projects_dir=args.claude_projects_dir,
        active_interval=args.active_interval,
        idle_interval=args.idle_interval,
        active_window=args.active_window,
    )

    asyncio.run(_run_with_signals(config))
    return 0


async def _run_with_signals(config: DaemonConfig) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass  # Windows

    await run(config, stop_event=stop_event)


if __name__ == "__main__":
    raise SystemExit(main())
