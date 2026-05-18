"""BurnScope daemon event loop.

Wires the watcher, probe scheduler, mDNS discovery, and HTTP pusher
together. Runs one or more `Agent` instances concurrently within a
single event loop: each agent has an independent probe cadence and its
own cached `AgentSnapshot`, so a probe failure in one agent does not
stall the others.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from . import discovery, pusher
from .agent import Agent
from .schema import AgentSnapshot
from .watcher import JsonlActivityWatcher

log = logging.getLogger(__name__)

DiscoverFn = Callable[[], Awaitable[str | None]]


@dataclass
class DaemonConfig:
    """Runtime configuration for `run()`.

    Fields:
        agents: One or more `Agent` instances to probe each cycle. The
            CLI builds this list either from `--agent` or by trying
            each known agent's credential loader.
        esp32_host: Pre-resolved ESP32 host (`"hostname"` or
            `"hostname:port"`). When `None`, mDNS discovery runs on
            startup and on push failure.
        claude_projects_dir: Directory the activity watcher tails for
            `.jsonl` changes to flip the probe cadence into "active".
        active_interval, idle_interval, active_window, tick: Cadence
            knobs (seconds).
        discovery_timeout: mDNS lookup timeout (seconds).
        watcher_poll_interval: How often the polling file watcher wakes
            up (seconds).
    """

    agents: list[Agent]
    esp32_host: str | None  # if set, mDNS is skipped
    claude_projects_dir: Path
    active_interval: float = 60.0
    idle_interval: float = 300.0
    active_window: float = 300.0
    tick: float = 5.0
    discovery_timeout: float = 10.0
    watcher_poll_interval: float = 1.0


@dataclass
class _AgentState:
    """Mutable per-agent state carried across loop iterations.

    `last_push_failed` is reset to `False` on every successful push and
    set `True` on every failure; the daemon uses it to decide whether
    to drop an mDNS-discovered host and rediscover.
    """

    last_probe_ts: float = 0.0
    last_snapshot: AgentSnapshot | None = None
    last_push_failed: bool = False


async def run(
    config: DaemonConfig,
    *,
    stop_event: asyncio.Event | None = None,
    discover_fn: DiscoverFn | None = None,
) -> None:
    """Run the daemon until `stop_event` is set.

    `discover_fn` is injectable for tests. Probes are driven by the
    `Agent` instances in `config.agents` — tests inject fake agents
    instead of patching a callable.
    """
    discover_fn = discover_fn or (
        lambda: discovery.discover_esp32(timeout=config.discovery_timeout)
    )
    stop_event = stop_event or asyncio.Event()

    if not config.agents:
        raise ValueError("DaemonConfig.agents must contain at least one agent")

    watcher = JsonlActivityWatcher(
        config.claude_projects_dir, poll_interval=config.watcher_poll_interval
    )
    watcher.start()

    states: dict[str, _AgentState] = {a.name: _AgentState() for a in config.agents}
    cached_host: str | None = config.esp32_host

    try:
        async with httpx.AsyncClient() as http:
            while not stop_event.is_set():
                now = time.time()
                interval = _current_interval(now, watcher.last_change_ts, config)

                for agent in config.agents:
                    state = states[agent.name]
                    if now - state.last_probe_ts < interval:
                        continue
                    try:
                        state.last_snapshot = await agent.probe(http)
                        state.last_probe_ts = now
                        log.info(
                            "probe ok (%s): %s",
                            agent.name,
                            ", ".join(
                                f"{s.type}={s.used_pct:.0%}"
                                for s in state.last_snapshot.sessions
                            ),
                        )
                    except Exception as exc:
                        log.warning("probe failed (%s): %s", agent.name, exc)
                        # Back off one tick before retrying this agent.
                        state.last_probe_ts = now - interval + config.tick

                if any(s.last_snapshot is not None for s in states.values()):
                    if cached_host is None:
                        cached_host = await discover_fn()
                        if cached_host is None:
                            log.warning(
                                "mDNS discovery timed out; "
                                "will retry next tick"
                            )
                    if cached_host is not None:
                        await _push_all(states, cached_host, http)
                        # Drop an mDNS-discovered host when every agent's
                        # push just failed, so we rediscover next tick.
                        if (
                            config.esp32_host is None
                            and _all_pushes_failed(states)
                        ):
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
    """Pick the active or idle probe cadence based on watcher activity."""
    if last_change_ts is None:
        return config.idle_interval
    if now - last_change_ts < config.active_window:
        return config.active_interval
    return config.idle_interval


async def _push_all(
    states: dict[str, _AgentState],
    host: str,
    http: httpx.AsyncClient,
) -> None:
    """Push the latest snapshot for every agent that has one."""
    for name, state in states.items():
        if state.last_snapshot is None:
            continue
        try:
            await pusher.push(state.last_snapshot, host, http)
            log.debug("pushed %s snapshot to %s", name, host)
            state.last_push_failed = False
        except Exception as exc:
            log.warning("push %s to %s failed: %s", name, host, exc)
            state.last_push_failed = True


def _all_pushes_failed(states: dict[str, _AgentState]) -> bool:
    """True iff every agent that has a snapshot just failed to push."""
    pushed = [s for s in states.values() if s.last_snapshot is not None]
    if not pushed:
        return False
    return all(s.last_push_failed for s in pushed)
