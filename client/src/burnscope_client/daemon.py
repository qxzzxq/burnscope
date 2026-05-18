"""BurnScope daemon event loop.

Wires the watcher, probe scheduler, mDNS discovery, and HTTP pusher together.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from . import claude, discovery, pusher
from .claude import AgentSnapshot
from .watcher import JsonlActivityWatcher

log = logging.getLogger(__name__)

ProbeFn = Callable[[str, httpx.AsyncClient], Awaitable[AgentSnapshot]]
DiscoverFn = Callable[[], Awaitable[str | None]]


@dataclass
class DaemonConfig:
    token: str
    esp32_host: str | None  # if set, mDNS is skipped
    claude_projects_dir: Path
    active_interval: float = 60.0
    idle_interval: float = 300.0
    active_window: float = 300.0
    tick: float = 5.0
    discovery_timeout: float = 10.0
    watcher_poll_interval: float = 1.0


async def run(
    config: DaemonConfig,
    *,
    stop_event: asyncio.Event | None = None,
    probe_fn: ProbeFn | None = None,
    discover_fn: DiscoverFn | None = None,
) -> None:
    """Run the daemon until `stop_event` is set.

    `probe_fn` and `discover_fn` are injectable for tests.
    """
    probe_fn = probe_fn or claude.probe
    discover_fn = discover_fn or (
        lambda: discovery.discover_esp32(timeout=config.discovery_timeout)
    )
    stop_event = stop_event or asyncio.Event()

    watcher = JsonlActivityWatcher(
        config.claude_projects_dir, poll_interval=config.watcher_poll_interval
    )
    watcher.start()

    last_probe_ts: float = 0.0
    last_snapshot: AgentSnapshot | None = None
    cached_host: str | None = config.esp32_host

    try:
        async with httpx.AsyncClient() as http:
            while not stop_event.is_set():
                now = time.time()
                interval = _current_interval(now, watcher.last_change_ts, config)

                if now - last_probe_ts >= interval:
                    try:
                        last_snapshot = await probe_fn(config.token, http)
                        last_probe_ts = now
                        log.info(
                            "probe ok: %s",
                            ", ".join(
                                f"{s.type}={s.used_pct:.0%}"
                                for s in last_snapshot.sessions
                            ),
                        )
                    except Exception as exc:
                        log.warning("probe failed: %s", exc)
                        # Back off one full tick before retrying.
                        last_probe_ts = now - interval + config.tick

                if last_snapshot is not None:
                    if cached_host is None:
                        cached_host = await discover_fn()
                        if cached_host is None:
                            log.warning(
                                "mDNS discovery timed out; "
                                "will retry next tick"
                            )
                    if cached_host is not None:
                        try:
                            await pusher.push(last_snapshot, cached_host, http)
                            log.debug("pushed snapshot to %s", cached_host)
                        except Exception as exc:
                            log.warning("push to %s failed: %s", cached_host, exc)
                            if config.esp32_host is None:
                                # Forget mDNS-discovered host; rediscover.
                                cached_host = None

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=config.tick)
                except asyncio.TimeoutError:
                    pass
    finally:
        watcher.stop()


def _current_interval(
    now: float, last_change_ts: float | None, config: DaemonConfig
) -> float:
    if last_change_ts is None:
        return config.idle_interval
    if now - last_change_ts < config.active_window:
        return config.active_interval
    return config.idle_interval
