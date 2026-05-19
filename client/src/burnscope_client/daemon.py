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

    Just the last successful probe and its timestamp — the firmware is the
    source of truth for "what's currently displayed", so we don't cache a
    `last_pushed_snapshot` here. Each cycle we read `/health` and re-POST
    when the firmware-side sessions diverge from `last_snapshot`, which
    self-heals after an ESP32 reboot, daemon restart, or any other event
    that desyncs the two sides.
    """

    last_probe_ts: float = 0.0
    last_snapshot: AgentSnapshot | None = None


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
                        health = await pusher.fetch_health(cached_host, http)
                        if health is None:
                            # Drop an mDNS-discovered host so we rediscover
                            # next tick; without /health we can't tell what
                            # the firmware holds.
                            if config.esp32_host is None:
                                cached_host = None
                        else:
                            all_failed = await _reconcile(
                                states, cached_host, http, health
                            )
                            if config.esp32_host is None and all_failed:
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


async def _reconcile(
    states: dict[str, _AgentState],
    host: str,
    http: httpx.AsyncClient,
    health: dict,
) -> bool:
    """Push every agent whose latest probe diverges from firmware-side state.

    Compares each `state.last_snapshot.sessions` against
    `health["agents"][name]["sessions"]`. A missing agent on the firmware
    side counts as a divergence — that's how we self-heal after an ESP32
    restart. Returns True iff every attempted push failed (the caller uses
    this to trigger mDNS rediscovery).
    """
    fw_agents = health.get("agents") or {}
    if not isinstance(fw_agents, dict):
        fw_agents = {}

    attempted = 0
    failed = 0
    for name, state in states.items():
        if state.last_snapshot is None:
            continue
        fw_entry = fw_agents.get(name) or {}
        fw_sessions = fw_entry.get("sessions") if isinstance(fw_entry, dict) else None
        if _sessions_match(state.last_snapshot.sessions, fw_sessions):
            continue
        attempted += 1
        try:
            await pusher.push(state.last_snapshot, host, http)
            log.debug("pushed %s snapshot to %s", name, host)
        except Exception as exc:
            log.warning("push %s to %s failed: %s", name, host, exc)
            failed += 1
    return attempted > 0 and failed == attempted


# Tolerance below the on-device display resolution. Avoids spurious
# re-pushes from float round-trips through the firmware's `float`
# storage and `%g` JSON formatting.
_USED_PCT_TOL = 1e-3


def _sessions_match(
    local: list,
    firmware: list | None,
) -> bool:
    """Content-equality for sessions, tolerant of float round-trip noise.

    `local` is a list of `SessionSnapshot`; `firmware` is the JSON-decoded
    `sessions` array from `GET /health`. Order is not significant — wire
    format says clients look up by `type`.
    """
    if firmware is None or not isinstance(firmware, list):
        return False
    if len(local) != len(firmware):
        return False
    local_by_type = {s.type: s for s in local}
    for entry in firmware:
        if not isinstance(entry, dict):
            return False
        t = entry.get("type")
        peer = local_by_type.get(t)
        if peer is None:
            return False
        try:
            fw_pct = float(entry.get("used_pct"))
            fw_reset = int(entry.get("resets_at"))
        except (TypeError, ValueError):
            return False
        if abs(peer.used_pct - fw_pct) > _USED_PCT_TOL:
            return False
        if peer.resets_at != fw_reset:
            return False
    return True
