"""Command-line entry point for the BurnScope client daemon.

Builds one or more `Agent` instances from `--agent` (or by auto-detect
across every known agent's credential loader) and hands them to the
daemon. Exits non-zero with a friendly message when no agent
credentials are available.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from . import discovery
from .agent import Agent
from .agents import ClaudeAgent, CodexAgent
from .daemon import DaemonConfig, run

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"

_AGENT_CLASSES: dict[str, type[Agent]] = {
    ClaudeAgent.name: ClaudeAgent,
    CodexAgent.name: CodexAgent,
}


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for `burnscope-client`."""
    p = argparse.ArgumentParser(
        prog="burnscope-client",
        description=(
            "Probe AI coding agents' rate-limit headers and push usage "
            "snapshots to an ESP32 display."
        ),
    )
    p.add_argument(
        "--agent",
        choices=sorted(_AGENT_CLASSES),
        action="append",
        help=(
            "Restrict to the named agent. Repeatable. "
            "Defaults to every agent whose credentials are present."
        ),
    )
    p.add_argument(
        "--esp32-host",
        help="Override mDNS discovery; format host or host:port (default port 80).",
    )
    p.add_argument(
        "--active-interval",
        type=float,
        default=None,
        help=(
            "Global override (seconds) for every agent's active-cadence default "
            "when recent activity is detected. Defaults to each agent's own value."
        ),
    )
    p.add_argument(
        "--idle-interval",
        type=float,
        default=None,
        help=(
            "Global override (seconds) for every agent's idle-cadence default "
            "when no recent activity. Defaults to each agent's own value."
        ),
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
    """Parse CLI args, build agents, and run the daemon until interrupted."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        agents = _build_agents(args.agent)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"burnscope-client: {exc}", file=sys.stderr)
        return 1

    host = discovery.parse_host_override(args.esp32_host) if args.esp32_host else None
    config = DaemonConfig(
        agents=agents,
        esp32_host=host,
        claude_projects_dir=args.claude_projects_dir,
        active_interval=args.active_interval,
        idle_interval=args.idle_interval,
        active_window=args.active_window,
    )

    asyncio.run(_run_with_signals(config))
    return 0


def _build_agents(requested: list[str] | None) -> list[Agent]:
    """Build agent instances either from `--agent` or by auto-detect.

    With `--agent`, every named agent must have credentials — missing
    credentials raise a clear `SystemExit`. Without `--agent`, every
    agent with credentials is included; if none are present, raise.
    """
    if requested:
        agents: list[Agent] = []
        for name in requested:
            cls = _AGENT_CLASSES[name]
            built = cls.try_create()
            if built is None:
                raise SystemExit(
                    f"burnscope-client: no credentials found for "
                    f"agent {name!r}. Log in first."
                )
            agents.append(built)
        return agents

    detected: list[Agent] = []
    for cls in _AGENT_CLASSES.values():
        built = cls.try_create()
        if built is not None:
            detected.append(built)
    if not detected:
        raise SystemExit(
            "burnscope-client: no agent credentials found. "
            "Log in to Claude Code or the Codex CLI first."
        )
    return detected


async def _run_with_signals(config: DaemonConfig) -> None:
    """Wire SIGINT/SIGTERM into a `stop_event` and call `daemon.run`."""
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
