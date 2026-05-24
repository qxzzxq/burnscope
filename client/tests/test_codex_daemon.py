"""Integration-ish tests for the Codex daemon — drive it with a fake
app-server subprocess so we can assert protocol behavior end-to-end.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from burnscope_client import codex_daemon, host_cache
from burnscope_client.codex_daemon import (
    CodexDaemon,
    _firmware_diverged,
    _snapshot_from_rate_limits,
)
from burnscope_client.host_cache import PairedDevice
from burnscope_client.pusher import PushResult
from burnscope_client.schema import AgentSnapshot, SessionSnapshot


# ============================================================ pure conversion


def test_snapshot_from_rate_limits_maps_both_windows():
    snap = _snapshot_from_rate_limits(
        {
            "primary": {"usedPercent": 23, "windowDurationMins": 300, "resetsAt": 1779066600},
            "secondary": {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": 1779156000},
        }
    )
    assert snap is not None
    assert snap.agent == "codex"
    types = {s.type for s in snap.sessions}
    assert types == {"primary", "secondary"}


def test_snapshot_from_rate_limits_skips_window_with_null_resets_at():
    snap = _snapshot_from_rate_limits(
        {
            "primary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": None},
            "secondary": {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": 1779156000},
        }
    )
    assert snap is not None
    assert [s.type for s in snap.sessions] == ["secondary"]


def test_snapshot_from_rate_limits_returns_none_when_all_null():
    snap = _snapshot_from_rate_limits(
        {"primary": None, "secondary": None}
    )
    assert snap is None


def test_snapshot_from_rate_limits_returns_none_for_non_dict():
    assert _snapshot_from_rate_limits(None) is None


def test_firmware_diverged_matches_exact_sessions():
    expected = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[SessionSnapshot("primary", 0.23, 1779066600)],
    )
    body_match = {
        "agents": {
            "codex": {
                "sessions": [
                    {"type": "primary", "used_pct": 0.23, "resets_at": 1779066600}
                ]
            }
        }
    }
    assert _firmware_diverged(body_match, expected) is False


def test_firmware_diverged_detects_missing_agent():
    expected = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[SessionSnapshot("primary", 0.23, 1779066600)],
    )
    assert _firmware_diverged({"agents": {}}, expected) is True
    assert _firmware_diverged({}, expected) is True


def test_firmware_diverged_detects_value_mismatch():
    expected = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[SessionSnapshot("primary", 0.23, 1779066600)],
    )
    body = {
        "agents": {
            "codex": {
                "sessions": [
                    {"type": "primary", "used_pct": 0.99, "resets_at": 1779066600}
                ]
            }
        }
    }
    assert _firmware_diverged(body, expected) is True


# ============================================================== fake server


class _FakeStream:
    """Minimal asyncio.StreamReader/Writer drop-in for one direction."""

    def __init__(self) -> None:
        self._buf = asyncio.Queue()
        self._closed = False

    async def readline(self) -> bytes:
        if self._closed and self._buf.empty():
            return b""
        item = await self._buf.get()
        if item is None:
            self._closed = True
            return b""
        return item

    def feed_line(self, payload: dict) -> None:
        self._buf.put_nowait((json.dumps(payload) + "\n").encode())

    def feed_eof(self) -> None:
        self._buf.put_nowait(None)

    # Writer side
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


class _FakeStdin:
    """Captures lines the daemon sends to the app-server."""

    def __init__(self) -> None:
        self._buf = b""
        self.lines: list[dict] = []

    def write(self, data: bytes) -> None:
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line:
                self.lines.append(json.loads(line))

    async def drain(self) -> None:
        pass


class _FakeProc:
    def __init__(self, stdout: _FakeStream, stdin: _FakeStdin) -> None:
        self.stdout = stdout
        self.stdin = stdin
        self.returncode = None
        self._terminated = False

    def terminate(self) -> None:
        self._terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return 0


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))


async def _drive_bootstrap(daemon: CodexDaemon, stdout: _FakeStream, stdin: _FakeStdin):
    """Run bootstrap() while feeding the app-server's responses."""

    async def feed():
        # Wait for initialize → respond
        await _wait_for_method(stdin, "initialize")
        stdout.feed_line(
            {
                "id": stdin.lines[-1]["id"],
                "result": {"userAgent": "test", "platformOs": "linux"},
            }
        )
        await _wait_for_method(stdin, "account/read")
        stdout.feed_line(
            {
                "id": stdin.lines[-1]["id"],
                "result": {"account": {"email": "user@example.com"}},
            }
        )
        await _wait_for_method(stdin, "account/rateLimits/read")
        stdout.feed_line(
            {
                "id": stdin.lines[-1]["id"],
                "result": {
                    "rateLimits": {
                        "primary": {
                            "usedPercent": 12,
                            "windowDurationMins": 300,
                            "resetsAt": 1779066600,
                        },
                        "secondary": {
                            "usedPercent": 30,
                            "windowDurationMins": 10080,
                            "resetsAt": 1779156000,
                        },
                    }
                },
            }
        )

    # The bootstrap awaits responses; reader_loop isn't running yet, so we
    # need to dispatch responses inline as the daemon awaits them.
    reader_task = asyncio.create_task(_responder(stdout, daemon))
    feeder = asyncio.create_task(feed())
    try:
        await daemon._bootstrap()
    finally:
        feeder.cancel()
        reader_task.cancel()
        with pytest.raises(BaseException):
            await reader_task


async def _wait_for_method(stdin: _FakeStdin, method: str):
    for _ in range(200):
        if stdin.lines and stdin.lines[-1].get("method") == method:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"daemon never sent {method}")


async def _responder(stdout: _FakeStream, daemon: CodexDaemon):
    """Forward lines from the fake stdout into the daemon's dispatcher."""
    while True:
        line = await stdout.readline()
        if not line:
            return
        msg = json.loads(line)
        daemon._dispatch(msg)


# ================================================================== tests


async def test_bootstrap_sends_initialize_then_account_then_ratelimits():
    daemon = CodexDaemon()
    stdout = _FakeStream()
    stdin = _FakeStdin()
    daemon._proc = _FakeProc(stdout, stdin)  # type: ignore[assignment]

    await _drive_bootstrap(daemon, stdout, stdin)

    methods = [line["method"] for line in stdin.lines]
    assert methods == ["initialize", "account/read", "account/rateLimits/read"]
    # Email is passed through plaintext, not hashed.
    assert daemon._client_id == "user@example.com"

    # Initial rateLimits/read result was enqueued for the pusher.
    queued = daemon._snapshot_queue.get_nowait()
    assert queued.agent == "codex"
    assert {s.type for s in queued.sessions} == {"primary", "secondary"}


async def test_run_once_drives_reader_concurrently_with_bootstrap(monkeypatch):
    """Regression: _run_once must run the reader alongside bootstrap.

    If the reader only starts after bootstrap completes, the initialize
    request's response is never read and the future times out — which is
    exactly what bit the live daemon ("initialize timed out").

    This test fails on the buggy ordering because bootstrap blocks on the
    initialize future, the reader never runs, and `_run_once` raises
    `initialize timed out` instead of advancing to EOF.
    """
    # Tight timeout so the test fails fast on regression instead of waiting 30s.
    monkeypatch.setattr(codex_daemon, "REQUEST_TIMEOUT_S", 1.0)

    daemon = CodexDaemon()
    stdout = _FakeStream()
    stdin = _FakeStdin()
    proc = _FakeProc(stdout, stdin)

    async def fake_spawn() -> None:
        daemon._proc = proc  # type: ignore[assignment]

    async def noop_loop() -> None:
        # Park forever — gather() will cancel us when the reader EOFs.
        await asyncio.Event().wait()

    monkeypatch.setattr(daemon, "_spawn", fake_spawn)
    monkeypatch.setattr(daemon, "_pusher_loop", noop_loop)
    monkeypatch.setattr(daemon, "_health_loop", noop_loop)

    async def feed_responses() -> None:
        await _wait_for_method(stdin, "initialize")
        stdout.feed_line({"id": stdin.lines[-1]["id"], "result": {"userAgent": "t"}})
        await _wait_for_method(stdin, "account/read")
        stdout.feed_line(
            {
                "id": stdin.lines[-1]["id"],
                "result": {"account": {"email": "u@example.com"}},
            }
        )
        await _wait_for_method(stdin, "account/rateLimits/read")
        stdout.feed_line(
            {
                "id": stdin.lines[-1]["id"],
                "result": {
                    "rateLimits": {
                        "primary": {
                            "usedPercent": 10,
                            "windowDurationMins": 300,
                            "resetsAt": 1779066600,
                        }
                    }
                },
            }
        )
        # Bootstrap is now done. End the connection so _run_once unwinds.
        stdout.feed_eof()

    feeder = asyncio.create_task(feed_responses())
    try:
        with pytest.raises(codex_daemon.CodexProtocolError) as ei:
            await daemon._run_once()
        # The expected failure is the reader hitting EOF, NOT a bootstrap
        # timeout — that's the whole point of this regression test.
        assert "EOF" in str(ei.value)
    finally:
        if not feeder.done():
            feeder.cancel()
            with pytest.raises(BaseException):
                await feeder

    assert daemon._client_id == "u@example.com"


async def test_rate_limits_updated_notification_enqueues_push():
    daemon = CodexDaemon()
    daemon._client_id = "x" * 64

    daemon._dispatch(
        {
            "method": "account/rateLimits/updated",
            "params": {
                "rateLimits": {
                    "primary": {
                        "usedPercent": 55,
                        "windowDurationMins": 300,
                        "resetsAt": 1779066600,
                    },
                    "secondary": None,
                }
            },
        }
    )

    snap = daemon._snapshot_queue.get_nowait()
    assert [s.type for s in snap.sessions] == ["primary"]
    assert snap.sessions[0].used_pct == pytest.approx(0.55)


async def test_unsolicited_response_id_is_ignored():
    daemon = CodexDaemon()
    # Should not raise — the daemon just logs and moves on.
    daemon._dispatch({"id": 9999, "result": {}})


async def test_dispatch_propagates_error_to_pending_request():
    daemon = CodexDaemon()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    daemon._pending[1] = codex_daemon._PendingRequest(fut)

    daemon._dispatch({"id": 1, "error": {"code": -32001, "message": "overloaded"}})

    with pytest.raises(codex_daemon.CodexProtocolError):
        await fut


async def _drain_one_push(daemon: CodexDaemon, *, predicate, max_iter=50):
    """Run the pusher loop briefly, until `predicate()` is True."""
    task = asyncio.create_task(daemon._pusher_loop())
    try:
        for _ in range(max_iter):
            if predicate():
                return
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_pusher_loop_fans_out_and_writes_per_device_state(monkeypatch):
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-a", "10.0.0.5:80"))
    host_cache.add_paired_device("codex", PairedDevice("dev-b", "10.0.0.6:80"))

    seen: list[tuple[str, ...]] = []

    async def fake_push_to_all(snapshot, devices, client_id, client):
        seen.append(tuple(d.device_id for d in devices))
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    daemon._enqueue_snapshot(
        AgentSnapshot(
            agent="codex",
            captured_at=1,
            sessions=[SessionSnapshot("primary", 0.5, 1779066600)],
        )
    )
    await _drain_one_push(
        daemon,
        predicate=lambda: host_cache.read_push_state("codex") is not None,
    )

    assert seen and set(seen[0]) == {"dev-a", "dev-b"}
    assert host_cache.read_push_state("codex")["ok"] is True
    assert host_cache.read_push_state("codex", device_id="dev-a")["ok"] is True
    assert host_cache.read_push_state("codex", device_id="dev-b")["ok"] is True


async def test_pusher_loop_silently_drops_device_on_401(monkeypatch):
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-keep", "10.0.0.5:80"))
    host_cache.add_paired_device("codex", PairedDevice("dev-drop", "10.0.0.6:80"))

    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {
            "dev-keep": PushResult("dev-keep", True,  "ok"),
            "dev-drop": PushResult("dev-drop", False, "auth"),
        }

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    daemon._enqueue_snapshot(
        AgentSnapshot(
            agent="codex",
            captured_at=1,
            sessions=[SessionSnapshot("primary", 0.5, 1779066600)],
        )
    )
    await _drain_one_push(
        daemon,
        predicate=lambda: any(
            d.device_id == "dev-drop"
            for d in host_cache.load_paired_devices("codex")
        ) is False,
    )

    remaining = {d.device_id for d in host_cache.load_paired_devices("codex")}
    assert remaining == {"dev-keep"}


async def test_pusher_loop_aggregate_false_when_all_devices_dropped(monkeypatch):
    # If every paired device returns 401 (or otherwise gets dropped), the
    # post-push state mirrors "no paired devices" — and `_push_one` writes
    # ok=False for that case at line ~340. The fan-out branch must agree.
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-a", "10.0.0.5:80"))
    host_cache.add_paired_device("codex", PairedDevice("dev-b", "10.0.0.6:80"))

    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {d.device_id: PushResult(d.device_id, False, "auth") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    daemon._enqueue_snapshot(
        AgentSnapshot(
            agent="codex",
            captured_at=1,
            sessions=[SessionSnapshot("primary", 0.5, 1779066600)],
        )
    )
    await _drain_one_push(
        daemon,
        predicate=lambda: host_cache.read_push_state("codex") is not None
        and not host_cache.load_paired_devices("codex"),
    )

    assert host_cache.load_paired_devices("codex") == []
    aggregate = host_cache.read_push_state("codex")
    assert aggregate is not None and aggregate["ok"] is False


async def test_pusher_loop_keeps_device_and_records_failure_on_transport(monkeypatch):
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-flaky", "10.0.0.5:80"))

    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {"dev-flaky": PushResult("dev-flaky", False, "transport")}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    daemon._enqueue_snapshot(
        AgentSnapshot(
            agent="codex",
            captured_at=1,
            sessions=[SessionSnapshot("primary", 0.5, 1779066600)],
        )
    )
    await _drain_one_push(
        daemon,
        predicate=lambda: host_cache.read_push_state("codex") is not None,
    )

    # Device stays in the paired list — transport failure isn't ownership.
    assert {d.device_id for d in host_cache.load_paired_devices("codex")} == {
        "dev-flaky"
    }
    assert host_cache.read_push_state("codex", device_id="dev-flaky")["ok"] is False
    assert host_cache.read_push_state("codex")["ok"] is False


async def test_pusher_loop_auto_pairs_when_empty(monkeypatch):
    """Empty paired list triggers discover_all + per-device claim."""
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"

    from burnscope_client.discovery import DiscoveredDevice

    async def fake_discover_all(timeout=10.0, agent=None, zc=None):
        return [DiscoveredDevice("dev-new", "10.0.0.5:80", False, False)]

    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)

    claimed = []

    async def fake_push(snap, host, client_id, client):
        claimed.append((host, client_id))

    monkeypatch.setattr(codex_daemon, "push", fake_push)

    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    daemon._enqueue_snapshot(
        AgentSnapshot(
            agent="codex",
            captured_at=1,
            sessions=[SessionSnapshot("primary", 0.5, 1779066600)],
        )
    )
    await _drain_one_push(
        daemon,
        predicate=lambda: any(
            d.device_id == "dev-new"
            for d in host_cache.load_paired_devices("codex")
        ),
    )

    assert claimed and claimed[0] == ("10.0.0.5:80", "u@example.com")
    assert {d.device_id for d in host_cache.load_paired_devices("codex")} == {
        "dev-new"
    }


def test_enqueue_bounded_caps_queue_and_keeps_newest():
    """Under sustained push failure the snapshot queue must stay bounded.

    Older snapshots are stale by definition once a newer one arrives, so
    when the queue is full we drop the oldest. After flooding the queue
    with N > cap snapshots, qsize() must equal the cap, and the items
    left must be the *newest* cap-many (FIFO with drop-oldest).
    """

    daemon = CodexDaemon()
    cap = codex_daemon.SNAPSHOT_QUEUE_MAX
    total = cap * 3
    for i in range(total):
        daemon._enqueue_bounded(
            AgentSnapshot(
                agent="codex",
                captured_at=i,  # use captured_at to identify each snap
                sessions=[SessionSnapshot("primary", 0.5, 1779066600)],
            )
        )

    assert daemon._snapshot_queue.qsize() == cap
    seen = []
    while not daemon._snapshot_queue.empty():
        seen.append(daemon._snapshot_queue.get_nowait().captured_at)
    assert seen == list(range(total - cap, total))
