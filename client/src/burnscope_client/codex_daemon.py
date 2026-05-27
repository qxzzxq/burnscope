"""Long-lived Codex daemon — owns the `codex app-server` subprocess.

Four coroutines run for the lifetime of one app-server connection:

  * Reader  — pulls JSONL lines off stdout, resolves outstanding requests
              by `id`, and translates `account/rateLimits/updated`
              notifications into AgentSnapshots that get enqueued for the
              pusher. (The notification path is preserved for free, but
              cross-process emission is unreliable; see Poll below.)
  * Pusher  — drains the snapshot queue, resolves the ESP32 host via the
              mDNS cache, POSTs with the cached client_id header, and
              writes the per-agent last-push state file.
  * Health  — periodic GET /health. On transport failure invalidates the
              host cache; on body/snapshot divergence (firmware lost state)
              re-enqueues the latest snapshot. Mirrors v1's edge-triggered
              reconciliation.
  * Poll    — every POLL_INTERVAL_S, calls `account/rateLimits/read`
              against our own app-server and enqueues a push only when
              the freshly-read snapshot differs from `_last_pushed_snapshot`.
              Authoritative trigger for changes that originate in other
              `codex` CLI processes (which never reach the long-lived
              app-server's notification stream).

If the app-server EOFs, all four coroutines unwind, the manager backs off
(1s exponential up to 60s), and re-bootstraps. Identity is re-derived on
each fresh connection.

See `docs/client-spec-v2.html` § 7 for the per-event lifecycle and the
codex-app-server spec for protocol details.
"""

from __future__ import annotations

# Allow `python path/to/codex_daemon.py` for ad-hoc debugging in addition to
# the canonical `python -m burnscope_client.codex_daemon` invocation used by
# the installed launchd/systemd unit.
if __package__ in (None, ""):
    import os
    import sys as _sys

    _sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    __package__ = "burnscope_client"

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import random  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass  # noqa: E402

import httpx  # noqa: E402

from . import host_cache  # noqa: E402
from ._log import configure_logging  # noqa: E402
from .discovery import discover_all  # noqa: E402
from .host_cache import PairedDevice  # noqa: E402
from .identity import redact_client_id  # noqa: E402
from .pusher import (  # noqa: E402
    PushAuthError,
    PushError,
    fetch_health,
    push,
    push_to_all,
    reconcile_duplicate_hosts,
    refresh_and_retry_transport_failures,
)
from .schema import AgentSnapshot, SessionSnapshot  # noqa: E402

log = logging.getLogger(__name__)


AGENT_NAME = "codex"
APP_SERVER_CMD = ("codex", "app-server")
CLIENT_NAME = "burnscope"
HEALTH_INTERVAL_S = 30.0
# Active poll of `account/rateLimits/read`. Required because the
# app-server only emits `rateLimits/updated` when its own in-process
# cache changes — and our long-lived app-server is a passive observer,
# so external `codex` CLI prompts never reach it. The poll is the
# authoritative trigger; we dedupe against `_last_pushed_snapshot` so
# unchanged ticks stay silent (and the firmware can idle).
POLL_INTERVAL_S = 60.0
DISCOVERY_TIMEOUT_S = 5.0
BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 60.0
REQUEST_TIMEOUT_S = 30.0
# Cap the pusher's pending-snapshot queue. If the firmware is in AP-mode
# fallback (or the LAN is down) and the app-server keeps producing
# `rateLimits/updated` notifications, the queue would otherwise grow
# without bound. We only care about the most recent state per agent, so
# bounded + drop-oldest is the right shape: a stale snapshot has zero
# value once a newer one arrives.
SNAPSHOT_QUEUE_MAX = 8


class CodexProtocolError(RuntimeError):
    """The app-server replied with an error or unexpected shape."""


@dataclass
class _PendingRequest:
    future: asyncio.Future


class CodexDaemon:
    """One instance per process. Reusable across reconnects."""

    def __init__(
        self,
        *,
        app_server_cmd: tuple[str, ...] = APP_SERVER_CMD,
        client_version: str = "0.2.0",
    ) -> None:
        self._app_server_cmd = app_server_cmd
        self._client_version = client_version
        self._next_id = 0
        self._pending: dict[int, _PendingRequest] = {}
        self._snapshot_queue: asyncio.Queue[AgentSnapshot] = asyncio.Queue(
            maxsize=SNAPSHOT_QUEUE_MAX
        )
        self._client_id: str | None = None
        self._last_snapshot: AgentSnapshot | None = None
        # The most recent snapshot we *successfully pushed* to at least one
        # device. Distinct from `_last_snapshot` (latest received from the
        # app-server, may not have made it through). Used by the poll loop
        # to decide whether a freshly-read snapshot represents a real
        # change — see `_poll_loop`.
        self._last_pushed_snapshot: AgentSnapshot | None = None
        self._proc: asyncio.subprocess.Process | None = None
        # Two diagnostic transport-failure counters, one per probe path.
        # Per the mDNS resilience plan, neither path evicts on transport
        # failure — only `/summary` 401 removes a pairing. The counters
        # stay split so a push success cannot zero out accumulated
        # health failures, and vice versa, keeping the per-path "have we
        # heard from this device recently" signal honest for logs and a
        # future `burnscope status` view.
        self._push_failures: dict[str, int] = {}
        self._health_failures: dict[str, int] = {}

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        """Run forever: spawn → bootstrap → coroutines → restart on EOF."""
        backoff = BACKOFF_INITIAL_S
        while True:
            try:
                await self._run_once()
                backoff = BACKOFF_INITIAL_S  # clean exit resets the timer
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("codex daemon iteration failed: %s", exc)

            sleep_for = backoff + random.uniform(0, backoff * 0.25)
            log.info("restarting codex app-server in %.1fs", sleep_for)
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 2.0, BACKOFF_MAX_S)

    async def _run_once(self) -> None:
        await self._spawn()
        try:
            # Reader must run concurrently with bootstrap — initialize's
            # response is read off stdout, so without the reader the future
            # never resolves and we hit REQUEST_TIMEOUT_S.
            await asyncio.gather(
                self._reader_loop(),
                self._bootstrap_then_workers(),
            )
        finally:
            self._fail_pending(CodexProtocolError("app-server connection ended"))
            await self._terminate()

    async def _bootstrap_then_workers(self) -> None:
        await self._bootstrap()
        await asyncio.gather(
            self._pusher_loop(),
            self._health_loop(),
            self._poll_loop(),
        )

    # ----------------------------------------------------------- subprocess

    async def _spawn(self) -> None:
        log.info("spawning %s", " ".join(self._app_server_cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *self._app_server_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _terminate(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    # ------------------------------------------------------------ bootstrap

    async def _bootstrap(self) -> None:
        await self._request(
            "initialize",
            {
                "clientInfo": {"name": CLIENT_NAME, "version": self._client_version},
                "capabilities": {
                    "experimentalApi": False,
                    "optOutNotificationMethods": [],
                },
            },
        )

        account = await self._request("account/read", {})
        email = _extract_email(account)
        if not email:
            raise CodexProtocolError("account/read returned no email")
        # The email itself is the plaintext wire-level identifier — no hash.
        cached = host_cache.read_client_id(AGENT_NAME)
        if cached != email:
            host_cache.write_client_id(AGENT_NAME, email)
        self._client_id = email
        log.debug("codex identifier resolved: %s", redact_client_id(email))

        rl = await self._request("account/rateLimits/read", {})
        snapshot = _snapshot_from_rate_limits(rl.get("rateLimits"))
        if snapshot is not None:
            self._enqueue_snapshot(snapshot)

    # ---------------------------------------------------------- request/req

    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _request(self, method: str, params: dict) -> dict:
        if self._proc is None or self._proc.stdin is None:
            raise CodexProtocolError("app-server stdin is not open")
        req_id = self._alloc_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = _PendingRequest(future)
        payload = {"id": req_id, "method": method, "params": params}
        self._proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self._proc.stdin.drain()
        try:
            result = await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise CodexProtocolError(f"{method} timed out") from exc
        if not isinstance(result, dict):
            raise CodexProtocolError(f"{method} returned non-object result")
        return result

    def _fail_pending(self, exc: BaseException) -> None:
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(exc)
        self._pending.clear()

    # --------------------------------------------------------------- reader

    async def _reader_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        while True:
            line = await stdout.readline()
            if not line:
                raise CodexProtocolError("app-server stdout EOF")
            try:
                msg = json.loads(line)
            except ValueError:
                log.warning("unparseable line from app-server: %r", line[:200])
                continue
            if not isinstance(msg, dict):
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg and "method" not in msg:
            pending = self._pending.pop(msg["id"], None)
            if pending is None:
                log.debug("response for unknown id %r", msg.get("id"))
                return
            if "error" in msg:
                pending.future.set_exception(
                    CodexProtocolError(f"app-server error: {msg['error']}")
                )
                return
            pending.future.set_result(msg.get("result") or {})
            return

        method = msg.get("method")
        if method == "account/rateLimits/updated":
            params = msg.get("params") or {}
            snapshot = _snapshot_from_rate_limits(params.get("rateLimits"))
            if snapshot is not None:
                self._enqueue_snapshot(snapshot)
        else:
            log.debug("ignoring notification %s", method)

    def _enqueue_snapshot(self, snapshot: AgentSnapshot) -> None:
        self._last_snapshot = snapshot
        self._enqueue_bounded(snapshot)
        log.debug(
            "enqueued codex snapshot (sessions=%d) for push",
            len(snapshot.sessions),
        )

    def _enqueue_bounded(self, snapshot: AgentSnapshot) -> None:
        """Put `snapshot` on the queue, evicting the oldest if it's full.

        The pusher only needs the latest state of the world; dropping an
        older queued snapshot to make room for a newer one is preferable
        to letting the queue grow without bound when pushes are stalled
        (e.g. firmware in AP-mode fallback).
        """
        try:
            self._snapshot_queue.put_nowait(snapshot)
        except asyncio.QueueFull:
            try:
                dropped = self._snapshot_queue.get_nowait()
                log.warning(
                    "snapshot queue full; dropping oldest (sessions=%d)",
                    len(dropped.sessions),
                )
            except asyncio.QueueEmpty:
                pass
            self._snapshot_queue.put_nowait(snapshot)

    # --------------------------------------------------------------- pusher

    async def _pusher_loop(self) -> None:
        host_cache.migrate_legacy_host_file()
        async with httpx.AsyncClient() as client:
            while True:
                snapshot = await self._snapshot_queue.get()
                await self._push_one(snapshot, client)

    async def _push_one(
        self, snapshot: AgentSnapshot, client: httpx.AsyncClient
    ) -> None:
        if self._client_id is None:
            log.warning("no client_id; skipping push")
            host_cache.write_push_state(AGENT_NAME, ok=False)
            return

        devices = await self._resolve_paired_devices(snapshot, client)
        if not devices:
            log.warning("no paired codex devices; skipping push")
            host_cache.write_push_state(AGENT_NAME, ok=False)
            return

        await self._push_to_devices(snapshot, devices, client)

    async def _push_to_devices(
        self,
        snapshot: AgentSnapshot,
        devices: list[PairedDevice],
        client: httpx.AsyncClient,
    ) -> None:
        """Fan out `snapshot` to `devices` and apply result bookkeeping.

        Factored out of `_push_one` so the health-loop can drive a
        targeted push (only the diverged devices) instead of going
        through the queue + fan-out-to-all path.

        Updates per-device push state, evicts only on 401 (transport
        failures are diagnostics-only per the mDNS resilience plan),
        advances `_last_pushed_snapshot` on any device's success, and
        writes the aggregate status.

        Caller must have already verified `self._client_id is not None`.
        """
        assert self._client_id is not None
        results = await push_to_all(snapshot, devices, self._client_id, client)
        results = await refresh_and_retry_transport_failures(
            snapshot, devices, results, self._client_id, client, AGENT_NAME,
            discovery_timeout=DISCOVERY_TIMEOUT_S,
        )
        # Identity check: if two paired records share the same cached
        # host after transport recovery, success against that host
        # can't prove which physical device replied. Devices in an
        # unresolved duplicate-host group are marked unverified below.
        current_devices = host_cache.load_paired_devices(AGENT_NAME)
        unverified = await reconcile_duplicate_hosts(
            AGENT_NAME, current_devices, discovery_timeout=DISCOVERY_TIMEOUT_S,
        )
        overall_ok = True
        any_ok = False
        kept = 0
        for device_id, result in results.items():
            effective_ok = result.ok and device_id not in unverified
            host_cache.write_push_state(
                AGENT_NAME, ok=effective_ok, device_id=device_id
            )
            if effective_ok:
                any_ok = True
            elif device_id in unverified:
                # Conflict unresolved this cycle. Bump the diagnostic
                # counter and keep the pairing — never evict on identity
                # ambiguity (plan §3).
                log.warning(
                    "device %s in unresolved duplicate-host group; marking unverified",
                    device_id,
                )
                self._push_failures[device_id] = (
                    self._push_failures.get(device_id, 0) + 1
                )
                overall_ok = False
                kept += 1
                continue
            if result.kind == "auth":
                log.info("dropping %s from codex paired list (401)", device_id)
                host_cache.remove_paired_device(AGENT_NAME, device_id)
                self._push_failures.pop(device_id, None)
                self._health_failures.pop(device_id, None)
                continue
            if result.kind == "transport":
                # Bump the diagnostics counter so logs and a future
                # `burnscope status` view can surface a flaky peer, but
                # DO NOT evict. Per the mDNS resilience plan, transport
                # failure is a reachability signal, not an ownership one
                # — the refresh-and-retry helper just ran one throttled
                # mDNS pass and the device gets another chance next
                # cycle. Only `/summary` 401 may remove a pairing here.
                self._push_failures[device_id] = (
                    self._push_failures.get(device_id, 0) + 1
                )
            else:
                # Push succeeded — clear only the push counter. Health
                # has its own counter and resets independently.
                self._push_failures.pop(device_id, None)
            kept += 1
            if not result.ok:
                overall_ok = False
        # If every device was dropped during this push, mirror the "no
        # paired devices" branch above and surface ok=False so `burnscope
        # status` doesn't report a misleading healthy aggregate.
        if kept == 0:
            overall_ok = False
        host_cache.write_push_state(AGENT_NAME, ok=overall_ok)
        # Advance the dedupe baseline as soon as *any* device accepted
        # the push — that device now has the snapshot, so re-pushing the
        # same content next minute would pummel a working peer because
        # of a flaky one. Failing devices fall behind by at most one
        # poll cycle until the next semantic change; transport-failed
        # peers get healed by the throttled mDNS reconciliation in
        # `refresh_and_retry_transport_failures` rather than evicted.
        # `overall_ok` is reserved for status reporting.
        if any_ok:
            self._last_pushed_snapshot = snapshot

    # ----------------------------------------------------------------- poll

    async def _poll_loop(self) -> None:
        """Actively poll `account/rateLimits/read` and dedupe before push.

        Required because the long-lived `codex app-server` doesn't watch
        `~/.codex/state_*.sqlite` (verified via lsof — zero file watchers),
        so rate-limit changes that happen in other `codex` CLI processes
        never reach it via `rateLimits/updated` notifications. The read
        call itself does re-read from disk on each invocation (verified
        empirically against a held app-server with mid-test CLI activity).

        Silent during steady-state — only enqueues when the freshly-read
        snapshot differs semantically from `_last_pushed_snapshot`. This
        is what lets the firmware's idle state machine reach the dimmed
        and off states.
        """
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            if self._client_id is None:
                continue
            try:
                result = await self._request("account/rateLimits/read", {})
            except CodexProtocolError as exc:
                log.debug("codex poll: read failed (%s); skipping iteration", exc)
                continue
            snapshot = _snapshot_from_rate_limits(result.get("rateLimits"))
            if snapshot is None:
                continue
            # Codex's backend reports `resetsAt` as roughly `now + remaining`,
            # so it slides ~60 s per 60 s of wall-clock even with zero
            # activity (verified empirically). Without anchoring the dedupe
            # gate fires on every poll and we wake the firmware out of
            # burn-in idle once a minute for no real change. Anchor each
            # session's `resets_at` to what we last pushed when `used_pct`
            # is unchanged — the firmware-side synthesis (when rolling and
            # used_pct ≤ 0.01) keeps the displayed countdown sensible.
            snapshot = _anchor_resets_at(snapshot, self._last_pushed_snapshot)
            if snapshot.semantically_equal(self._last_pushed_snapshot):
                log.debug("codex poll: rate limits unchanged; skipping push")
                continue
            log.info("codex poll: rate limits changed; enqueueing push")
            self._enqueue_snapshot(snapshot)

    # --------------------------------------------------------------- health

    async def _health_loop(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                await asyncio.sleep(HEALTH_INTERVAL_S)
                if self._client_id is None:
                    continue
                devices = host_cache.load_paired_devices(AGENT_NAME)
                if not devices:
                    continue
                diverged_devices: list[PairedDevice] = []
                failed_devices: list[PairedDevice] = []
                ok_devices: list[PairedDevice] = []
                for device in devices:
                    body = await fetch_health(device.host, self._client_id, client)
                    if body is None:
                        failed_devices.append(device)
                        continue
                    # Identity verification (plan §8c): firmware ≥0.5.x
                    # echoes `device_id` in /health. A mismatch means we
                    # reached someone else's device at the cached host —
                    # treat as a stale-host conflict so the mDNS reconcile
                    # path runs. Older firmware omits the field; absence
                    # is not a mismatch (backwards-compatible fallback).
                    actual_id = body.get("device_id")
                    if (
                        isinstance(actual_id, str)
                        and actual_id != device.device_id
                    ):
                        log.warning(
                            "health: %s replied with device_id=%s (expected %s); "
                            "treating as identity conflict",
                            device.host, actual_id, device.device_id,
                        )
                        failed_devices.append(device)
                        continue
                    ok_devices.append(device)
                    self._mark_health_ok(device, body, diverged_devices)
                healed: set[str] = set()
                if failed_devices:
                    healed = await self._reconcile_health_failures(
                        failed_devices, client, diverged_devices,
                    )
                # Identity check after round 2: if two paired records
                # still share a cached host, success at that host can't
                # prove which physical device replied. Mark each as
                # ok=False so the aggregate is honest until reconciliation
                # can split them (plan §8b).
                current_devices = host_cache.load_paired_devices(AGENT_NAME)
                unverified = await reconcile_duplicate_hosts(
                    AGENT_NAME, current_devices,
                    discovery_timeout=DISCOVERY_TIMEOUT_S,
                )
                if unverified:
                    for device_id in unverified:
                        log.warning(
                            "health: device %s in unresolved duplicate-host group; marking unverified",
                            device_id,
                        )
                        host_cache.write_push_state(
                            AGENT_NAME, ok=False, device_id=device_id
                        )
                any_ok = (
                    any(d.device_id not in unverified for d in ok_devices)
                    or bool(healed - unverified)
                )
                # Aggregate ok=False when nothing was verifiably healthy
                # in either round (initial probe or post-reconcile retry).
                if not any_ok:
                    host_cache.write_push_state(AGENT_NAME, ok=False)
                if diverged_devices and self._last_pushed_snapshot is not None:
                    # Re-push only to the device(s) that actually diverged,
                    # not the whole fleet. Non-diverged peers don't need
                    # the update; pushing to them would wake their idle
                    # state machine and waste bandwidth.
                    log.info(
                        "firmware diverged on %d device(s); re-pushing to %s",
                        len(diverged_devices),
                        [d.device_id for d in diverged_devices],
                    )
                    await self._push_to_devices(
                        self._last_pushed_snapshot, diverged_devices, client
                    )

    def _mark_health_ok(
        self,
        device: PairedDevice,
        body: dict,
        diverged_devices: list[PairedDevice],
    ) -> None:
        """Record a successful health probe — clear the counter, then
        check whether the firmware diverged from `_last_pushed_snapshot`.

        Compare against `_last_pushed_snapshot` (the anchored value the
        firmware actually has), not `_last_snapshot` (the raw value from
        the app-server). Otherwise poll-loop anchoring would manufacture
        a spurious divergence here every cycle.
        """
        self._health_failures.pop(device.device_id, None)
        if (
            self._last_pushed_snapshot is not None
            and _firmware_diverged(body, self._last_pushed_snapshot)
        ):
            diverged_devices.append(device)

    def _record_health_failure(self, device: PairedDevice) -> None:
        """Persist a health-probe failure for `device` without evicting.

        Per the mDNS resilience plan (§8) the pairing is preserved
        regardless of how many health probes miss — only `/summary` 401
        is an ownership signal. The counter still advances so logs (and
        a future `burnscope status` view) can surface the degraded peer.
        """
        host_cache.write_push_state(
            AGENT_NAME, ok=False, device_id=device.device_id
        )
        self._health_failures[device.device_id] = (
            self._health_failures.get(device.device_id, 0) + 1
        )

    async def _reconcile_health_failures(
        self,
        failed: list[PairedDevice],
        client: httpx.AsyncClient,
        diverged_devices: list[PairedDevice],
    ) -> set[str]:
        """Run one throttled mDNS browse for the failed batch, retry
        `/health` at any refreshed host, and record outcomes.

        Returns the set of device_ids that healed in round 2. The
        caller uses it (minus any duplicate-host-unverified ids) to
        decide whether the aggregate may remain ok. Devices inside
        their per-device cooldown are recorded as failed without
        launching a new browse — that's the whole point of
        `claim_reconcile_slots`.
        """
        failed_ids = [d.device_id for d in failed]
        eligible = host_cache.claim_reconcile_slots(AGENT_NAME, failed_ids)
        if not eligible:
            for device in failed:
                self._record_health_failure(device)
            return set()

        try:
            discovered = await discover_all(timeout=DISCOVERY_TIMEOUT_S)
        except Exception as exc:
            # mDNS failures must not kill the long-lived health loop.
            log.warning("mDNS browse during health reconcile failed: %s", exc)
            for device in failed:
                self._record_health_failure(device)
            return set()
        by_id = {d.device_id: d for d in discovered}

        healed: set[str] = set()
        for device in failed:
            if device.device_id not in eligible:
                # Inside cooldown — record failure, don't try mDNS.
                self._record_health_failure(device)
                continue
            found = by_id.get(device.device_id)
            if found is None or found.host == device.host:
                # mDNS didn't see it (or same host) — failure stands.
                self._record_health_failure(device)
                continue
            if not host_cache.update_paired_device_host(
                AGENT_NAME, device.device_id, found.host
            ):
                # Pairing was removed during reconcile (e.g. /summary 401
                # in the pusher) — drop without resurrecting.
                log.info(
                    "skipped health retry for %s — pairing was removed during reconcile",
                    device.device_id,
                )
                continue
            log.info(
                "health host changed for %s (%s -> %s); retrying",
                device.device_id, device.host, found.host,
            )
            refreshed = PairedDevice(
                device_id=device.device_id, host=found.host
            )
            retry = await fetch_health(refreshed.host, self._client_id, client)
            if retry is None:
                self._record_health_failure(refreshed)
                continue
            healed.add(refreshed.device_id)
            host_cache.write_push_state(
                AGENT_NAME, ok=True, device_id=refreshed.device_id
            )
            self._mark_health_ok(refreshed, retry, diverged_devices)
        return healed

    # ----------------------------------------------------------- device list

    async def _resolve_paired_devices(
        self, snapshot: AgentSnapshot, client: httpx.AsyncClient
    ) -> list[PairedDevice]:
        """Steady state: read the cache. First run: discover + claim."""
        cached = host_cache.load_paired_devices(AGENT_NAME)
        if cached:
            log.debug("paired-devices.%s hit (%d device(s))", AGENT_NAME, len(cached))
            return cached
        log.info("paired-devices.%s empty; running auto-pair discovery", AGENT_NAME)
        assert self._client_id is not None
        # Unfiltered so devices already TOFU-bound to us (paired_<agent>=1
        # with our client_id) get re-claimed after a paired-devices.json
        # wipe. Mismatches are silently skipped on 401 below.
        discovered = await discover_all(timeout=DISCOVERY_TIMEOUT_S)
        claimed: list[PairedDevice] = []
        for device in discovered:
            candidate = PairedDevice(device_id=device.device_id, host=device.host)
            try:
                await push(snapshot, candidate.host, self._client_id, client)
            except PushAuthError:
                log.info("auto-pair skipped %s (401)", candidate.device_id)
                continue
            except PushError as exc:
                log.info(
                    "auto-pair skipped %s (transport): %s", candidate.device_id, exc
                )
                continue
            log.info("auto-pair claimed %s at %s", candidate.device_id, candidate.host)
            host_cache.add_paired_device(AGENT_NAME, candidate)
            host_cache.write_push_state(
                AGENT_NAME, ok=True, device_id=candidate.device_id
            )
            claimed.append(candidate)
        return claimed


# ============================================================== module-level


def _extract_email(account: dict) -> str | None:
    """Pull `account.email` out of an `account/read` response.

    The app-server has shipped variations — try a couple of shapes before
    giving up.
    """
    for path in (("account", "email"), ("email",), ("user", "email")):
        node: object = account
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str) and node:
            return node
    return None


def _anchor_resets_at(
    fresh: AgentSnapshot,
    last: AgentSnapshot | None,
) -> AgentSnapshot:
    """Return `fresh` with each session's `resets_at` rewritten to the
    matching last-pushed session's value when `used_pct` is unchanged.

    Codex's rolling windows report `resetsAt` as roughly `now + remaining`,
    so it advances ~60 s per 60 s of wall-clock at low usage. Pushing that
    drift to the firmware every minute defeats the burn-in-mitigation
    idle state machine. Anchoring keeps the wire-level `resets_at` stable
    until the daemon observes a real change (`used_pct`, `rolling`,
    `window_duration_mins`, or the set of session types). The firmware
    extrapolates from there: when `rolling=True` and `used_pct ≤ 0.01`,
    it synthesizes `now + window_duration_mins * 60` so the displayed
    countdown stays sensible even as the anchored `resets_at` ages.

    Anchoring keys on `(type, used_pct)`. Any other field difference
    (rolling, window_duration_mins) flows through unmodified — those are
    contract-level changes and should reach the firmware immediately.
    """
    if last is None:
        return fresh
    last_by_type = {s.type: s for s in last.sessions}
    anchored: list[SessionSnapshot] = []
    for s in fresh.sessions:
        prior = last_by_type.get(s.type)
        if prior is None or prior.used_pct != s.used_pct:
            anchored.append(s)
            continue
        anchored.append(
            SessionSnapshot(
                type=s.type,
                used_pct=s.used_pct,
                resets_at=prior.resets_at,
                rolling=s.rolling,
                window_duration_mins=s.window_duration_mins,
            )
        )
    return AgentSnapshot(
        agent=fresh.agent,
        captured_at=fresh.captured_at,
        sessions=anchored,
    )


def _snapshot_from_rate_limits(rate_limits: object) -> AgentSnapshot | None:
    """Convert a RateLimitSnapshot dict to an AgentSnapshot.

    Skips a window when any of usedPercent, windowDurationMins, or resetsAt
    is missing/null. Empty result → returns None (no push).

    Both codex windows are rolling against wall-clock — verified
    empirically against a held app-server: `resetsAt` advances ~60 s per
    60 s of real time at low usage. We mark them as such on the wire so
    the firmware can synthesize a fresh countdown locally during idle
    (`rolling=True` + the window's duration in minutes).
    """
    if not isinstance(rate_limits, dict):
        return None

    sessions: list[SessionSnapshot] = []
    for label, key in (("primary", "primary"), ("secondary", "secondary")):
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        pct = window.get("usedPercent")
        resets = window.get("resetsAt")
        duration = window.get("windowDurationMins")
        if not isinstance(pct, (int, float)):
            continue
        if not isinstance(resets, int):
            continue
        if not isinstance(duration, int):
            continue
        sessions.append(
            SessionSnapshot(
                type=label,
                used_pct=float(pct) / 100.0,
                resets_at=int(resets),
                rolling=True,
                window_duration_mins=int(duration),
            )
        )

    if not sessions:
        return None
    return AgentSnapshot(
        agent=AGENT_NAME,
        captured_at=int(time.time()),
        sessions=sessions,
    )


def _firmware_diverged(health_body: dict, expected: AgentSnapshot) -> bool:
    """True if the firmware's per-agent sessions don't match `expected`.

    Conservative comparison — any structural mismatch counts as divergence
    and triggers a re-push. Exact equality on the JSON-ified shape so we
    don't get fooled by float-vs-int or order differences in `sessions`.

    Must include every field the firmware echoes back in /health
    (`rolling`, `window_duration_mins` were added in the wire-format
    extension); otherwise dict equality always fails on a key-count
    mismatch and the health loop re-pushes every cycle.
    """
    by_agent = health_body.get("agents")
    if not isinstance(by_agent, dict):
        return True
    stored = by_agent.get(expected.agent)
    if not isinstance(stored, dict):
        return True
    stored_sessions = stored.get("sessions")
    expected_sessions = [
        {
            "type": s.type,
            "used_pct": s.used_pct,
            "resets_at": s.resets_at,
            "rolling": s.rolling,
            "window_duration_mins": s.window_duration_mins,
        }
        for s in expected.sessions
    ]
    return stored_sessions != expected_sessions


# ============================================================== entry point


def main(argv: list[str] | None = None) -> int:
    # Stderr fallback so a stock launchd/systemd install still captures logs
    # via the supervisor's stdout/stderr redirect. BURNSCOPE_LOG_FILE wins
    # when set.
    configure_logging(fallback_stderr=True)
    daemon = CodexDaemon()
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
