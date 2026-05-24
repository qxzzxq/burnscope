"""Long-lived Codex daemon — owns the `codex app-server` subprocess.

Three coroutines run for the lifetime of one app-server connection:

  * Reader  — pulls JSONL lines off stdout, resolves outstanding requests
              by `id`, and translates `account/rateLimits/updated`
              notifications into AgentSnapshots that get enqueued for the
              pusher.
  * Pusher  — drains the snapshot queue, resolves the ESP32 host via the
              mDNS cache, POSTs with the cached client_id header, and
              writes the per-agent last-push state file.
  * Health  — periodic GET /health. On transport failure invalidates the
              host cache; on body/snapshot divergence (firmware lost state)
              re-enqueues the latest snapshot. Mirrors v1's edge-triggered
              reconciliation.

If the app-server EOFs, all three coroutines unwind, the manager backs off
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
    refresh_and_retry_transport_failures,
)
from .schema import AgentSnapshot, SessionSnapshot  # noqa: E402

log = logging.getLogger(__name__)


AGENT_NAME = "codex"
APP_SERVER_CMD = ("codex", "app-server")
CLIENT_NAME = "burnscope"
HEALTH_INTERVAL_S = 30.0
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
MAX_TRANSPORT_FAILURES = 5


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
        self._proc: asyncio.subprocess.Process | None = None
        self._transport_failures: dict[str, int] = {}

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

        results = await push_to_all(snapshot, devices, self._client_id, client)
        results = await refresh_and_retry_transport_failures(
            snapshot, devices, results, self._client_id, client, AGENT_NAME,
            discovery_timeout=DISCOVERY_TIMEOUT_S,
        )
        overall_ok = True
        kept = 0
        for device_id, result in results.items():
            host_cache.write_push_state(
                AGENT_NAME, ok=result.ok, device_id=device_id
            )
            if result.kind == "auth":
                log.info("dropping %s from codex paired list (401)", device_id)
                host_cache.remove_paired_device(AGENT_NAME, device_id)
                self._transport_failures.pop(device_id, None)
                continue
            if result.kind == "transport":
                failures = self._transport_failures.get(device_id, 0) + 1
                self._transport_failures[device_id] = failures
                if failures >= MAX_TRANSPORT_FAILURES:
                    log.warning(
                        "dropping %s after %d transport failures",
                        device_id, MAX_TRANSPORT_FAILURES,
                    )
                    host_cache.remove_paired_device(AGENT_NAME, device_id)
                    self._transport_failures.pop(device_id, None)
                    continue
            else:
                self._transport_failures.pop(device_id, None)
            kept += 1
            if not result.ok:
                overall_ok = False
        # If every device was dropped during this push, mirror the "no
        # paired devices" branch above and surface ok=False so `burnscope
        # status` doesn't report a misleading healthy aggregate.
        if kept == 0:
            overall_ok = False
        host_cache.write_push_state(AGENT_NAME, ok=overall_ok)

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
                diverged_any = False
                all_unreachable = True
                for device in devices:
                    body = await fetch_health(device.host, self._client_id, client)
                    if body is None:
                        host_cache.write_push_state(
                            AGENT_NAME, ok=False, device_id=device.device_id
                        )
                        failures = (
                            self._transport_failures.get(device.device_id, 0) + 1
                        )
                        self._transport_failures[device.device_id] = failures
                        if failures >= MAX_TRANSPORT_FAILURES:
                            log.warning(
                                "dropping %s after %d health failures",
                                device.device_id, MAX_TRANSPORT_FAILURES,
                            )
                            host_cache.remove_paired_device(AGENT_NAME, device.device_id)
                            self._transport_failures.pop(device.device_id, None)
                        continue
                    all_unreachable = False
                    self._transport_failures.pop(device.device_id, None)
                    if self._last_snapshot is not None and _firmware_diverged(
                        body, self._last_snapshot
                    ):
                        diverged_any = True
                if all_unreachable:
                    host_cache.write_push_state(AGENT_NAME, ok=False)
                if diverged_any and self._last_snapshot is not None:
                    log.info(
                        "firmware diverged on >=1 device; re-enqueuing last snapshot"
                    )
                    self._enqueue_bounded(self._last_snapshot)

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


def _snapshot_from_rate_limits(rate_limits: object) -> AgentSnapshot | None:
    """Convert a RateLimitSnapshot dict to an AgentSnapshot.

    Skips a window when any of usedPercent, windowDurationMins, or resetsAt
    is missing/null. Empty result → returns None (no push).
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
        sessions.append(SessionSnapshot(label, float(pct) / 100.0, int(resets)))

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
    """
    by_agent = health_body.get("agents")
    if not isinstance(by_agent, dict):
        return True
    stored = by_agent.get(expected.agent)
    if not isinstance(stored, dict):
        return True
    stored_sessions = stored.get("sessions")
    expected_sessions = [
        {"type": s.type, "used_pct": s.used_pct, "resets_at": s.resets_at}
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
