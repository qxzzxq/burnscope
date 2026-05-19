"""BurnScope daemon event loop.

Wires the probe scheduler, mDNS discovery, and HTTP pusher together.
Runs one or more `Agent` instances concurrently within a single event
loop: each agent declares its own `probe_interval` (the CLI's
`--probe-interval` flag acts as a global override) and caches its own
`AgentSnapshot`, so a probe failure in one agent does not stall the
others.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from . import discovery, pusher
from .agent import Agent, AuthError
from .credentials import CredentialsError
from .schema import AgentSnapshot

log = logging.getLogger(__name__)

DiscoverFn = Callable[[], Awaitable[str | None]]

# Per-agent backoff after a failed probe. The first failure waits
# `_BACKOFF_BASE` seconds; each subsequent consecutive failure multiplies
# the wait by `_BACKOFF_FACTOR`. After `_MAX_CONSECUTIVE_FAILURES` strikes
# the agent is parked (no further probes) until the daemon restarts.
_BACKOFF_BASE: float = 5.0
_BACKOFF_FACTOR: float = 3.0
_MAX_CONSECUTIVE_FAILURES: int = 5


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
        probe_interval: Global cadence override (seconds). When `None`,
            each agent's own class attribute is used; when set, every
            agent uses this value instead.
        tick: How often the loop wakes to evaluate per-agent cadences
            (seconds).
        discovery_timeout: mDNS lookup timeout (seconds).
    """

    agents: list[Agent]
    esp32_host: str | None  # if set, mDNS is skipped
    probe_interval: float | None = None
    tick: float = 5.0
    discovery_timeout: float = 10.0


@dataclass
class _AgentState:
    """Mutable per-agent state carried across loop iterations.

    Holds the last successful probe and its timestamp (the firmware is the
    source of truth for "what's currently displayed", so we don't cache a
    `last_pushed_snapshot`), plus a small failure-tracking trio:

    - `consecutive_failures`: count of probe failures since the last
      success. Reset on success.
    - `cooldown_until`: monotonic wall-clock seconds before which the
      next probe must not run; set after each failure to back off
      exponentially.
    - `stopped`: latched once `consecutive_failures` reaches
      `_MAX_CONSECUTIVE_FAILURES`. A stopped agent is skipped forever
      until the daemon restarts.
    """

    last_probe_ts: float = 0.0
    last_snapshot: AgentSnapshot | None = None
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    stopped: bool = False


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

    states: dict[str, _AgentState] = {a.name: _AgentState() for a in config.agents}
    cached_host: str | None = config.esp32_host

    async with httpx.AsyncClient() as http:
        while not stop_event.is_set():
            now = time.time()

            for agent in config.agents:
                state = states[agent.name]
                if state.stopped:
                    continue
                if now < state.cooldown_until:
                    continue
                interval = _current_interval(config, agent)
                if now - state.last_probe_ts < interval:
                    continue
                try:
                    state.last_snapshot = await agent.probe(http)
                    state.last_probe_ts = now
                    state.consecutive_failures = 0
                    state.cooldown_until = 0.0
                    log.info(
                        "probe ok (%s): %s",
                        agent.name,
                        ", ".join(
                            f"{s.type}={s.used_pct:.0%}"
                            for s in state.last_snapshot.sessions
                        ),
                    )
                except Exception as exc:
                    _handle_probe_failure(agent, state, exc, now)

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


def _handle_probe_failure(
    agent: Agent,
    state: _AgentState,
    exc: BaseException,
    now: float,
) -> None:
    """Record one probe failure: refresh credential on auth, back off, latch.

    Bumps `consecutive_failures` and parks the agent until
    `now + _BACKOFF_BASE * _BACKOFF_FACTOR ** (n-1)`. If `exc` is an
    `AuthError`, also asks the agent to re-read its credential (a credential
    reload failure does not count as a separate strike — the next probe will
    just fail again with the stale token and bump the count then). Once the
    failure count hits `_MAX_CONSECUTIVE_FAILURES`, the agent is stopped and
    no longer probed until the daemon restarts.
    """
    state.consecutive_failures += 1
    n = state.consecutive_failures

    if isinstance(exc, AuthError):
        log.warning(
            "probe failed (%s) [%d/%d, auth]: %s; reloading credential",
            agent.name, n, _MAX_CONSECUTIVE_FAILURES, exc,
        )
        try:
            agent.reload_credential()
        except CredentialsError as reload_exc:
            log.warning(
                "credential reload failed (%s): %s", agent.name, reload_exc,
            )
    else:
        log.warning(
            "probe failed (%s) [%d/%d]: %s",
            agent.name, n, _MAX_CONSECUTIVE_FAILURES, exc,
        )

    if n >= _MAX_CONSECUTIVE_FAILURES:
        state.stopped = True
        log.error(
            "agent %s stopped after %d consecutive failures; "
            "will not probe again until daemon restart",
            agent.name, n,
        )
        return

    backoff = _BACKOFF_BASE * (_BACKOFF_FACTOR ** (n - 1))
    state.cooldown_until = now + backoff


def _current_interval(config: DaemonConfig, agent: Agent) -> float:
    """Pick the probe cadence for one agent.

    `DaemonConfig.probe_interval` acts as a global override when set;
    otherwise the agent's own class attribute wins.
    """
    if config.probe_interval is not None:
        return config.probe_interval
    return agent.probe_interval


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
