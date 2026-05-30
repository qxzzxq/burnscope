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
    _anchor_resets_at,
    _firmware_diverged,
    _snapshot_from_rate_limits,
)
from burnscope_client.host_cache import PairedDevice
from burnscope_client.pusher import PushResult
from burnscope_client.schema import AgentSnapshot, SessionSnapshot


# ============================================================ client version


def test_codex_daemon_client_version_defaults_to_package_version():
    """The version reported to the app-server's clientInfo is single-sourced
    from package metadata, not a separate hard-coded literal (PR #68 / Codex
    P2). Otherwise the daemon logs one version but identifies itself to Codex
    with a stale one. An explicit override is still honored.
    """
    import burnscope_client

    assert CodexDaemon()._client_version == burnscope_client.__version__
    assert CodexDaemon(client_version="9.9.9")._client_version == "9.9.9"


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


def test_snapshot_from_rate_limits_marks_both_windows_rolling_with_duration():
    """Codex windows are rolling against wall-clock; the wire fields must
    reflect that so the firmware can synthesize during idle."""
    snap = _snapshot_from_rate_limits(
        {
            "primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 1779066600},
            "secondary": {"usedPercent": 3, "windowDurationMins": 10080, "resetsAt": 1779156000},
        }
    )
    assert snap is not None
    by_type = {s.type: s for s in snap.sessions}
    assert by_type["primary"].rolling is True
    assert by_type["primary"].window_duration_mins == 300
    assert by_type["secondary"].rolling is True
    assert by_type["secondary"].window_duration_mins == 10080


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


# =============================================================== anchoring


def _codex_session(
    used_pct: float, resets_at: int, *, type_: str = "primary"
) -> SessionSnapshot:
    return SessionSnapshot(
        type=type_,
        used_pct=used_pct,
        resets_at=resets_at,
        rolling=True,
        window_duration_mins=300,
    )


def test_anchor_resets_at_returns_fresh_when_no_baseline():
    fresh = AgentSnapshot(
        agent="codex",
        captured_at=10,
        sessions=[_codex_session(0.01, 1779066600)],
    )
    anchored = _anchor_resets_at(fresh, None)
    assert anchored is fresh


def test_anchor_resets_at_preserves_resets_at_when_used_pct_unchanged():
    """The core drift-defeat behavior: same used_pct → keep last's resets_at."""
    last = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[_codex_session(0.01, 1779000000)],
    )
    fresh = AgentSnapshot(
        agent="codex",
        captured_at=2,
        sessions=[_codex_session(0.01, 1779000060)],
    )
    anchored = _anchor_resets_at(fresh, last)
    assert anchored.sessions[0].resets_at == 1779000000
    # used_pct, rolling, duration must still come from `fresh`.
    assert anchored.sessions[0].used_pct == 0.01
    assert anchored.sessions[0].rolling is True
    assert anchored.sessions[0].window_duration_mins == 300


def test_anchor_resets_at_uses_fresh_when_used_pct_changed():
    last = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[_codex_session(0.01, 1779000000)],
    )
    fresh = AgentSnapshot(
        agent="codex",
        captured_at=2,
        sessions=[_codex_session(0.02, 1779000060)],
    )
    anchored = _anchor_resets_at(fresh, last)
    # Real change → keep the fresh resets_at so the firmware re-anchors.
    assert anchored.sessions[0].used_pct == 0.02
    assert anchored.sessions[0].resets_at == 1779000060


def test_anchor_resets_at_passes_through_unmatched_session_type():
    """A session type that didn't exist in `last` flows through unchanged."""
    last = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[_codex_session(0.01, 1779000000, type_="primary")],
    )
    fresh = AgentSnapshot(
        agent="codex",
        captured_at=2,
        sessions=[
            _codex_session(0.01, 1779000060, type_="primary"),
            _codex_session(0.03, 1779999999, type_="secondary"),
        ],
    )
    anchored = _anchor_resets_at(fresh, last)
    by_type = {s.type: s for s in anchored.sessions}
    assert by_type["primary"].resets_at == 1779000000  # anchored
    assert by_type["secondary"].resets_at == 1779999999  # passed through


def test_firmware_diverged_matches_exact_sessions():
    expected = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[
            SessionSnapshot(
                type="primary",
                used_pct=0.23,
                resets_at=1779066600,
                rolling=True,
                window_duration_mins=300,
            )
        ],
    )
    # Firmware /health echoes back every wire field; the comparison must
    # match all of them or the health loop re-pushes every cycle.
    body_match = {
        "agents": {
            "codex": {
                "sessions": [
                    {
                        "type": "primary",
                        "used_pct": 0.23,
                        "resets_at": 1779066600,
                        "rolling": True,
                        "window_duration_mins": 300,
                    }
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
        sessions=[
            SessionSnapshot(
                type="primary",
                used_pct=0.23,
                resets_at=1779066600,
                rolling=True,
                window_duration_mins=300,
            )
        ],
    )
    body = {
        "agents": {
            "codex": {
                "sessions": [
                    {
                        "type": "primary",
                        "used_pct": 0.99,
                        "resets_at": 1779066600,
                        "rolling": True,
                        "window_duration_mins": 300,
                    }
                ]
            }
        }
    }
    assert _firmware_diverged(body, expected) is True


def test_firmware_diverged_detects_missing_wire_fields():
    """An old firmware that doesn't echo `rolling`/`window_duration_mins`
    must be flagged as diverged so the daemon re-pushes — keeping the
    behaviour conservative until the firmware is flashed with the
    matching wire-format extension.

    Also locks in the regression that broke in production: when a new
    firmware echoes the new fields but the daemon's diff key omitted
    them, every health probe manufactured a spurious divergence.
    """
    expected = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[
            SessionSnapshot(
                type="primary",
                used_pct=0.23,
                resets_at=1779066600,
                rolling=True,
                window_duration_mins=300,
            )
        ],
    )
    body_old_firmware = {
        "agents": {
            "codex": {
                "sessions": [
                    {"type": "primary", "used_pct": 0.23, "resets_at": 1779066600}
                ]
            }
        }
    }
    assert _firmware_diverged(body_old_firmware, expected) is True


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
    # mDNS finds nothing → no refresh/retry, original transport failure stands.
    from burnscope_client import pusher

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return []
    monkeypatch.setattr(pusher, "discover_all", fake_discover_all)

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


async def test_pusher_loop_recovers_when_device_ip_changed(monkeypatch):
    """Stale cached host → push transport-fails → mDNS rediscovers device
    at new host → cache updated and retry pushes to the new host.
    """
    from burnscope_client import pusher
    from burnscope_client.discovery import DiscoveredDevice

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-moved", "10.0.0.5:80"))

    seen_hosts: list[str] = []

    async def fake_push_to_all(snapshot, devices, client_id, client):
        # First call sees the stale host and reports transport failure.
        # The refresh helper then calls back into pusher.push_to_all
        # directly (not this monkeypatched alias), so we need to stub
        # the symbol used inside pusher.py too — see below.
        seen_hosts.extend(d.host for d in devices)
        return {d.device_id: PushResult(d.device_id, False, "transport") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    async def pusher_push_to_all(snapshot, devices, client_id, client):
        # The retry inside the helper goes through pusher.push_to_all.
        # New host succeeds.
        seen_hosts.extend(d.host for d in devices)
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(pusher, "push_to_all", pusher_push_to_all)

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return [DiscoveredDevice("dev-moved", "10.0.0.9:80", True, False)]
    monkeypatch.setattr(pusher, "discover_all", fake_discover_all)

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
        and host_cache.read_push_state("codex")["ok"] is True,
    )

    assert "10.0.0.5:80" in seen_hosts  # initial fan-out hit the stale host
    assert "10.0.0.9:80" in seen_hosts  # retry hit the refreshed host
    assert host_cache.load_paired_devices("codex") == [
        PairedDevice("dev-moved", "10.0.0.9:80")
    ]
    assert host_cache.read_push_state("codex")["ok"] is True
    # Push-failure counter must NOT advance — the failure was healed.
    assert daemon._push_failures.get("dev-moved", 0) == 0


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


async def _drive_poll_loop(
    daemon: CodexDaemon, *, until, max_iter: int = 200
) -> None:
    """Run `_poll_loop` until `until()` returns True or we time out."""
    task = asyncio.create_task(daemon._poll_loop())
    try:
        for _ in range(max_iter):
            if until():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("poll-loop predicate never satisfied")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _fake_request_returning(rate_limits: dict | None):
    """Build a stub for `daemon._request` that always returns the same result.

    Lets poll-loop tests skip the full stdin/stdout dance and focus on the
    dedupe + enqueue logic.
    """

    async def fake(method: str, params: dict) -> dict:
        assert method == "account/rateLimits/read"
        return {"rateLimits": rate_limits} if rate_limits is not None else {}

    return fake


async def test_poll_loop_first_iteration_always_enqueues(monkeypatch):
    """First poll fires with `_last_pushed_snapshot is None` → must enqueue.

    This is the guard against an installed daemon being silent forever if
    the bootstrap push failed.
    """
    monkeypatch.setattr(codex_daemon, "POLL_INTERVAL_S", 0.005)

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    monkeypatch.setattr(
        daemon,
        "_request",
        _fake_request_returning(
            {
                "primary": {
                    "usedPercent": 12,
                    "windowDurationMins": 300,
                    "resetsAt": 1779066600,
                }
            }
        ),
    )

    await _drive_poll_loop(daemon, until=lambda: daemon._snapshot_queue.qsize() >= 1)
    snap = daemon._snapshot_queue.get_nowait()
    assert [s.type for s in snap.sessions] == ["primary"]
    assert snap.sessions[0].used_pct == pytest.approx(0.12)


async def test_poll_loop_enqueues_when_rate_limits_change(monkeypatch):
    monkeypatch.setattr(codex_daemon, "POLL_INTERVAL_S", 0.005)

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    # Baseline: a previous push went out at 12%.
    daemon._last_pushed_snapshot = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[SessionSnapshot("primary", 0.12, 1779066600)],
    )
    # Read now returns 15% — different from baseline.
    monkeypatch.setattr(
        daemon,
        "_request",
        _fake_request_returning(
            {
                "primary": {
                    "usedPercent": 15,
                    "windowDurationMins": 300,
                    "resetsAt": 1779066600,
                }
            }
        ),
    )

    await _drive_poll_loop(daemon, until=lambda: daemon._snapshot_queue.qsize() >= 1)
    snap = daemon._snapshot_queue.get_nowait()
    assert snap.sessions[0].used_pct == pytest.approx(0.15)


async def test_poll_loop_skips_when_rate_limits_unchanged(monkeypatch):
    monkeypatch.setattr(codex_daemon, "POLL_INTERVAL_S", 0.005)

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    # Baseline must mirror the shape `_snapshot_from_rate_limits` produces
    # (rolling=True, window_duration_mins=300) so the dedupe key matches.
    daemon._last_pushed_snapshot = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[
            SessionSnapshot(
                type="primary",
                used_pct=0.12,
                resets_at=1779066600,
                rolling=True,
                window_duration_mins=300,
            )
        ],
    )
    monkeypatch.setattr(
        daemon,
        "_request",
        _fake_request_returning(
            {
                "primary": {
                    "usedPercent": 12,
                    "windowDurationMins": 300,
                    "resetsAt": 1779066600,
                }
            }
        ),
    )

    # Let several poll iterations run; queue must stay empty.
    task = asyncio.create_task(daemon._poll_loop())
    try:
        await asyncio.sleep(0.05)  # >= ~10 iterations at 5 ms cadence
        assert daemon._snapshot_queue.empty()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_poll_loop_silent_on_resets_at_drift_only(monkeypatch):
    """Anchoring must defeat codex's wall-clock-driven `resets_at` drift.

    Same `used_pct` but a sliding `resets_at` (the exact pattern observed
    against a live app-server at low usage) must not result in a push —
    otherwise the firmware wakes out of burn-in idle every poll.
    """
    monkeypatch.setattr(codex_daemon, "POLL_INTERVAL_S", 0.005)

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    daemon._last_pushed_snapshot = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[
            SessionSnapshot(
                type="primary",
                used_pct=0.01,
                resets_at=1779000000,
                rolling=True,
                window_duration_mins=300,
            )
        ],
    )
    # Same used_pct, resets_at slid forward by 60s — the exact symptom.
    monkeypatch.setattr(
        daemon,
        "_request",
        _fake_request_returning(
            {
                "primary": {
                    "usedPercent": 1,
                    "windowDurationMins": 300,
                    "resetsAt": 1779000060,
                }
            }
        ),
    )

    task = asyncio.create_task(daemon._poll_loop())
    try:
        await asyncio.sleep(0.05)
        assert daemon._snapshot_queue.empty()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_poll_loop_skips_when_client_id_missing(monkeypatch):
    """Before bootstrap completes (`_client_id is None`), poll must no-op."""
    monkeypatch.setattr(codex_daemon, "POLL_INTERVAL_S", 0.005)

    daemon = CodexDaemon()
    daemon._client_id = None

    called = False

    async def fake_request(method: str, params: dict) -> dict:
        nonlocal called
        called = True
        return {"rateLimits": {}}

    monkeypatch.setattr(daemon, "_request", fake_request)

    task = asyncio.create_task(daemon._poll_loop())
    try:
        await asyncio.sleep(0.05)
        assert called is False
        assert daemon._snapshot_queue.empty()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_poll_loop_tolerates_request_errors(monkeypatch):
    """A protocol error from `read` must not kill the loop."""
    monkeypatch.setattr(codex_daemon, "POLL_INTERVAL_S", 0.005)

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"

    call_count = 0

    async def flaky_request(method: str, params: dict) -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise codex_daemon.CodexProtocolError("transient")
        return {
            "rateLimits": {
                "primary": {
                    "usedPercent": 33,
                    "windowDurationMins": 300,
                    "resetsAt": 1779066600,
                }
            }
        }

    monkeypatch.setattr(daemon, "_request", flaky_request)

    await _drive_poll_loop(daemon, until=lambda: daemon._snapshot_queue.qsize() >= 1)
    assert call_count >= 2  # first failed, second succeeded


async def test_push_one_updates_last_pushed_snapshot_on_success(monkeypatch):
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-a", "10.0.0.5:80"))

    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    snap = AgentSnapshot(
        agent="codex",
        captured_at=42,
        sessions=[SessionSnapshot("primary", 0.5, 1779066600)],
    )
    daemon._enqueue_snapshot(snap)
    await _drain_one_push(
        daemon,
        predicate=lambda: daemon._last_pushed_snapshot is not None,
    )

    assert daemon._last_pushed_snapshot is snap


async def test_push_one_keeps_last_pushed_snapshot_unchanged_on_failure(monkeypatch):
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-drop", "10.0.0.5:80"))

    async def fake_push_to_all(snapshot, devices, client_id, client):
        # All devices fail with 401 → device dropped, overall_ok=False.
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
        predicate=lambda: host_cache.read_push_state("codex") is not None,
    )

    assert daemon._last_pushed_snapshot is None


async def test_push_one_advances_last_pushed_snapshot_on_partial_success(monkeypatch):
    """One device accepts, one fails → the snapshot was delivered, so the
    dedupe baseline must advance. Otherwise the working device would get
    re-pushed every minute until the flaky peer either recovers or gets
    evicted — exactly what the dedupe is meant to prevent.
    """
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-ok", "10.0.0.5:80"))
    host_cache.add_paired_device("codex", PairedDevice("dev-flaky", "10.0.0.6:80"))

    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {
            "dev-ok":    PushResult("dev-ok",    True,  "ok"),
            "dev-flaky": PushResult("dev-flaky", False, "transport"),
        }

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)
    # Block the refresh-and-retry helper from healing the transport failure,
    # so partial-success persists into _push_one's accounting.
    from burnscope_client import pusher

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return []
    monkeypatch.setattr(pusher, "discover_all", fake_discover_all)

    snap = AgentSnapshot(
        agent="codex",
        captured_at=42,
        sessions=[SessionSnapshot("primary", 0.5, 1779066600)],
    )
    daemon._enqueue_snapshot(snap)
    await _drain_one_push(
        daemon,
        predicate=lambda: daemon._last_pushed_snapshot is not None,
    )

    # Baseline advanced — dev-ok got the snapshot, so a follow-up poll
    # with the same data must dedupe out instead of pummeling dev-ok.
    assert daemon._last_pushed_snapshot is snap
    # Aggregate `ok` still reports the truth that one device is unhealthy.
    assert host_cache.read_push_state("codex")["ok"] is False
    # Flaky peer is still paired (transport failure, not auth) and its
    # diagnostic push-failure counter has advanced by one. Per the mDNS
    # resilience plan the counter never drives eviction — it's just for
    # logs and `burnscope status` to surface degraded peers. The health
    # counter is independent and should still be zero (no health probe
    # ran in this test).
    assert daemon._push_failures.get("dev-flaky") == 1
    assert daemon._health_failures.get("dev-flaky", 0) == 0
    assert {d.device_id for d in host_cache.load_paired_devices("codex")} == {
        "dev-ok",
        "dev-flaky",
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


# ============================================== H-2: split push/health counters


async def test_push_counter_does_not_reset_health_counter(monkeypatch):
    """A push success must not clear the health failure counter, and
    vice versa. Without separated counters, either path's success
    masked the other's accumulated failures (deep-review H-2).
    """
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-x", "10.0.0.5:80"))
    # Pretend the health loop already saw 3 health failures.
    daemon._health_failures["dev-x"] = 3

    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {"dev-x": PushResult("dev-x", True, "ok")}

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
        predicate=lambda: daemon._last_pushed_snapshot is not None,
    )

    # Push success cleared its own counter but the health counter
    # must remain at 3 — health still hasn't seen recovery.
    assert daemon._push_failures.get("dev-x", 0) == 0
    assert daemon._health_failures.get("dev-x") == 3


async def test_health_loop_keeps_device_after_many_health_failures(monkeypatch):
    """Per the mDNS resilience plan (§8), health failures must NEVER evict
    a pairing. Only /summary 401 is an ownership signal. A device that
    fails GET /health forever stays paired and shows up as degraded in
    `burnscope status` so the user can investigate.
    """
    from burnscope_client import pusher as pusher_mod

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-quiet", "10.0.0.5:80"))

    async def fake_fetch_health(host, client_id, client):
        return None  # always transport-fails

    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.001)

    # Health loop's mDNS reconciliation path must also find nothing.
    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return []
    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)
    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        for _ in range(500):
            if daemon._health_failures.get("dev-quiet", 0) >= 10:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("counter never reached 10 misses")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Counter advanced past the old eviction threshold; device still paired.
    assert daemon._health_failures["dev-quiet"] >= 10
    paired = {d.device_id for d in host_cache.load_paired_devices("codex")}
    assert paired == {"dev-quiet"}


async def test_health_loop_recovers_via_mdns_when_ip_changes(monkeypatch):
    """Stale cached IP → /health fails → one throttled mDNS browse
    rediscovers the device at its new host → retry /health at the new
    host succeeds, the cache is updated, and the failure counter resets.
    Without this path the codex daemon would keep probing the stale IP
    forever even though the device is alive on the LAN."""
    from burnscope_client.discovery import DiscoveredDevice

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-moved", "10.0.0.5:80"))

    health_calls: list[str] = []

    async def fake_fetch_health(host, client_id, client):
        health_calls.append(host)
        if host == "10.0.0.5:80":
            return None
        return {"agents": {}}

    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return [DiscoveredDevice("dev-moved", "10.0.0.9:80", True, False)]
    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        for _ in range(400):
            cached = host_cache.load_paired_devices("codex")
            if cached and cached[0].host == "10.0.0.9:80":
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("health loop never refreshed cached host")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert host_cache.load_paired_devices("codex") == [
        PairedDevice("dev-moved", "10.0.0.9:80")
    ]
    # Counter cleared by successful retry.
    assert daemon._health_failures.get("dev-moved", 0) == 0
    assert "10.0.0.5:80" in health_calls  # initial probe hit stale host
    assert "10.0.0.9:80" in health_calls  # retry hit refreshed host


async def test_health_loop_throttles_repeated_mdns_browses(monkeypatch):
    """Repeated health failures inside the cooldown window must not
    trigger repeated mDNS browses for the same offline device. The
    `claim_reconcile_slots` gate keeps a powered-off display from
    forcing a full LAN browse every HEALTH_INTERVAL_S."""
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-down", "10.0.0.5:80"))

    async def always_fail(host, client_id, client):
        return None
    monkeypatch.setattr(codex_daemon, "fetch_health", always_fail)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.001)

    browses = 0

    async def counting_discover(timeout=4.0, agent=None, zc=None):
        nonlocal browses
        browses += 1
        return []
    monkeypatch.setattr(codex_daemon, "discover_all", counting_discover)

    task = asyncio.create_task(daemon._health_loop())
    try:
        # Wait for the counter to accumulate well past 1 — proves multiple
        # health cycles ran. Default cooldown 60 s should still gate them
        # to a single browse.
        for _ in range(500):
            if daemon._health_failures.get("dev-down", 0) >= 5:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("never accumulated 5 health failures")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert browses == 1, (
        f"5+ health failures should yield 1 throttled browse, got {browses}"
    )


async def test_health_loop_keeps_pairing_when_unpaired_during_reconcile(monkeypatch):
    """If a concurrent /summary 401 or pair-reset removes the device
    while the health-loop's mDNS browse is in flight, the refreshed
    host must NOT be written back — update_paired_device_host returns
    False and the retry is dropped. Same anti-resurrection guarantee
    we get on the push side."""
    from burnscope_client.discovery import DiscoveredDevice

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-moved", "10.0.0.5:80"))

    async def fake_fetch_health(host, client_id, client):
        return None  # always fail for this test
    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    discovered_once = False

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        nonlocal discovered_once
        if not discovered_once:
            discovered_once = True
            # Simulate the race: pairing removed while mDNS browses.
            host_cache.remove_paired_device("codex", "dev-moved")
        return [DiscoveredDevice("dev-moved", "10.0.0.9:80", True, False)]

    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        for _ in range(200):
            if discovered_once:
                break
            await asyncio.sleep(0.005)
        # Let one more cycle pass so we observe post-reconcile state.
        await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # The pairing stays removed — no resurrection by the stale browse.
    assert host_cache.load_paired_devices("codex") == []


# ============================== mDNS resilience §5/§8b: duplicate-host conflict


async def test_pusher_loop_marks_duplicate_host_devices_unverified(monkeypatch):
    """Codex push: two records share the same cached host; HTTP returns
    204 for both but we can't prove which device replied. Per plan §5
    the per-device state must not be ok=True for either, and the
    aggregate must reflect the unresolved conflict."""
    from burnscope_client import pusher as pusher_mod

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-a", "10.0.0.5:80"))
    host_cache.add_paired_device("codex", PairedDevice("dev-b", "10.0.0.5:80"))

    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}
    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return []
    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)

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

    # Both still paired (no eviction on conflict).
    paired = {d.device_id for d in host_cache.load_paired_devices("codex")}
    assert paired == {"dev-a", "dev-b"}
    # Neither claimed healthy from the shared HTTP response.
    assert host_cache.read_push_state("codex", device_id="dev-a")["ok"] is False
    assert host_cache.read_push_state("codex", device_id="dev-b")["ok"] is False
    assert host_cache.read_push_state("codex")["ok"] is False


async def test_health_loop_treats_device_id_mismatch_as_identity_conflict(
    monkeypatch,
):
    """/health succeeded but the responding device's identity doesn't
    match the paired record — we reached the wrong physical device.
    Per plan §8c, treat as stale-host conflict: feed the device through
    the mDNS reconciliation path, do not mark it healthy."""
    from burnscope_client.discovery import DiscoveredDevice

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device(
        "codex", PairedDevice("burnscope-aaaa", "10.0.0.5:80")
    )

    async def fake_fetch_health(host, client_id, client):
        # Whoever is at 10.0.0.5:80 reports a different device_id —
        # cached host points at someone else's device now.
        return {"device_id": "burnscope-bbbb", "agents": {}}
    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    # mDNS finds the real device at a new host so the reconciliation
    # path can heal the cache.
    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return [DiscoveredDevice("burnscope-aaaa", "10.0.0.9:80", True, False)]
    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        for _ in range(400):
            cached = host_cache.load_paired_devices("codex")
            if cached and cached[0].host == "10.0.0.9:80":
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError(
                "health loop never refreshed host after identity mismatch"
            )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Cache refreshed to the right device.
    assert host_cache.load_paired_devices("codex") == [
        PairedDevice("burnscope-aaaa", "10.0.0.9:80")
    ]


async def test_health_loop_accepts_response_without_device_id(monkeypatch):
    """Backwards compatibility: older firmware doesn't emit device_id.
    Absence of the field must not be treated as a mismatch — the daemon
    falls back to local duplicate-host detection alone (plan §4
    rollout note)."""
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device(
        "codex", PairedDevice("burnscope-aaaa", "10.0.0.5:80")
    )

    async def fake_fetch_health(host, client_id, client):
        # Pre-0.5.x firmware shape: no device_id field.
        return {"agents": {}}
    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    discovered = False

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        nonlocal discovered
        discovered = True
        return []
    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        # Wait long enough for several cycles to run.
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Pairing untouched, no mDNS reconcile triggered (the probe was
    # treated as successful because device_id absence ≠ mismatch).
    assert host_cache.load_paired_devices("codex") == [
        PairedDevice("burnscope-aaaa", "10.0.0.5:80")
    ]
    assert discovered is False, (
        "absence of device_id must not trigger mDNS reconciliation"
    )
    # Health counter must remain at 0 — no failure was recorded.
    assert daemon._health_failures.get("burnscope-aaaa", 0) == 0


async def test_health_loop_marks_duplicate_host_devices_unverified(monkeypatch):
    """Codex health: two paired records share the same cached host. Even
    if /health succeeds at that host, neither device may be reported as
    healthy — per plan §8b the loop must run a throttled reconciliation
    and surface the unresolved conflict as ok=False."""
    from burnscope_client import pusher as pusher_mod

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-a", "10.0.0.5:80"))
    host_cache.add_paired_device("codex", PairedDevice("dev-b", "10.0.0.5:80"))

    async def fake_fetch_health(host, client_id, client):
        return {"agents": {}}
    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return []
    # Stub both call sites: codex_daemon.discover_all for the health
    # reconciliation path, pusher.discover_all for the duplicate-host
    # reconciliation path. Without both, the dup-host helper performs
    # a real ~4 s mDNS browse and the test times out.
    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)
    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        for _ in range(200):
            state_a = host_cache.read_push_state("codex", device_id="dev-a")
            state_b = host_cache.read_push_state("codex", device_id="dev-b")
            if state_a is not None and state_b is not None:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("health loop never wrote per-device state")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Both still paired.
    paired = {d.device_id for d in host_cache.load_paired_devices("codex")}
    assert paired == {"dev-a", "dev-b"}
    # Neither marked ok=True from the shared health response.
    assert host_cache.read_push_state("codex", device_id="dev-a")["ok"] is False
    assert host_cache.read_push_state("codex", device_id="dev-b")["ok"] is False


# ====================== mDNS resilience §7: push transport failures don't evict


async def test_pusher_loop_keeps_device_after_many_push_transport_failures(
    monkeypatch,
):
    """Codex push path must not evict a device after many transport
    misses. Reachability failure is not ownership loss — the device
    may have been rebooted, roamed networks, or moved IP. Keep it
    paired and let the throttled mDNS reconciliation heal it.

    Counter still bumps for diagnostics (so `burnscope status` can show
    the degraded device), but no `remove_paired_device` call is made on
    transport failures.
    """
    from burnscope_client import pusher as pusher_mod

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-flaky", "10.0.0.5:80"))

    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {d.device_id: PushResult(d.device_id, False, "transport") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)
    # Block the refresh path from healing the failure.
    monkeypatch.setattr(pusher_mod, "push_to_all", fake_push_to_all)

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return []
    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)

    # Fire well past the old eviction threshold.
    for i in range(10):
        daemon._enqueue_snapshot(
            AgentSnapshot(
                agent="codex",
                captured_at=i,
                sessions=[SessionSnapshot("primary", 0.5, 1779066600)],
            )
        )
        await _drain_one_push(
            daemon,
            predicate=lambda i=i: daemon._push_failures.get("dev-flaky", 0) >= i + 1,
        )

    paired = {d.device_id for d in host_cache.load_paired_devices("codex")}
    assert paired == {"dev-flaky"}, "transport failures must not evict"
    assert daemon._push_failures["dev-flaky"] == 10  # counter still advances


# =================================================== L-6: per-device divergence


async def test_health_divergence_pushes_only_to_diverged_device(monkeypatch):
    """When one of two paired devices shows divergence, the re-push
    must target only that device — not fan out to the healthy peer
    and wake its idle SM (deep-review L-6).
    """
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-good", "10.0.0.5:80"))
    host_cache.add_paired_device("codex", PairedDevice("dev-diverged", "10.0.0.6:80"))

    # Daemon's idea of what the firmware *should* have — the health
    # divergence baseline is `_last_pushed_snapshot` (post-anchoring,
    # this is the value the firmware actually received), not the raw
    # `_last_snapshot` which may carry an un-anchored resets_at from the
    # app-server.
    snap = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[
            SessionSnapshot(
                type="primary",
                used_pct=0.5,
                resets_at=1779066600,
                rolling=True,
                window_duration_mins=300,
            )
        ],
    )
    daemon._last_pushed_snapshot = snap

    async def fake_fetch_health(host, client_id, client):
        if host == "10.0.0.5:80":
            # dev-good already has the snapshot — must echo every wire
            # field or `_firmware_diverged` flags it as out-of-sync.
            return {
                "agents": {
                    "codex": {
                        "sessions": [
                            {
                                "type": "primary",
                                "used_pct": 0.5,
                                "resets_at": 1779066600,
                                "rolling": True,
                                "window_duration_mins": 300,
                            }
                        ]
                    }
                }
            }
        # dev-diverged lost state (reboot)
        return {"agents": {}}

    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    pushed_to: list[list[str]] = []

    async def fake_push_to_all(snapshot, devices, client_id, client):
        pushed_to.append([d.device_id for d in devices])
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    from burnscope_client import pusher as pusher_mod

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return []
    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        for _ in range(200):
            if pushed_to:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("health loop never re-pushed")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Re-push went to dev-diverged ONLY — not the healthy dev-good.
    assert pushed_to[0] == ["dev-diverged"]


# ---------------------------------------------- health-loop 401 re-bind (#66)


async def _rebind_snapshot() -> AgentSnapshot:
    return AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[
            SessionSnapshot(
                type="primary",
                used_pct=0.5,
                resets_at=1779066600,
                rolling=True,
                window_duration_mins=300,
            )
        ],
    )


async def test_health_loop_rebinds_slot_on_401(monkeypatch):
    """An idle codex daemon whose paired device rebooted / wiped NVS gets a
    401 from /health (its slot is empty, another agent's slot is populated).
    The loop must re-push `_last_pushed_snapshot` so /summary TOFU re-binds
    the empty slot — issue #66. Without this the slot never re-binds until
    codex usage changes or the daemon restarts.
    """
    from burnscope_client.discovery import DiscoveredDevice

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-reboot", "10.0.0.5:80"))
    daemon._last_pushed_snapshot = await _rebind_snapshot()

    async def fake_fetch_health(host, client_id, client):
        return codex_daemon.HEALTH_AUTH_REJECTED

    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    pushed_to: list[list[tuple[str, str]]] = []

    async def fake_push_to_all(snapshot, devices, client_id, client):
        pushed_to.append([(d.device_id, d.host) for d in devices])
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    from burnscope_client import pusher as pusher_mod

    # mDNS confirms the device is still at its cached host (rebooted in place).
    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return [DiscoveredDevice("dev-reboot", "10.0.0.5:80", True, False)]
    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)
    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        for _ in range(200):
            if pushed_to:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("health loop never re-pushed to re-bind the slot")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # The re-bind push targeted the 401 device at its mDNS-confirmed host.
    assert pushed_to[0] == [("dev-reboot", "10.0.0.5:80")]
    # Device stays paired (TOFU re-claim succeeded).
    paired = {d.device_id for d in host_cache.load_paired_devices("codex")}
    assert paired == {"dev-reboot"}


async def test_health_loop_skips_rebind_when_no_last_pushed_snapshot(monkeypatch):
    """A 401 with nothing to re-push (daemon never pushed yet) must not
    fabricate a push — there's no snapshot to TOFU-bind with. The pusher /
    poll loop reclaims once it produces a snapshot. Per-device state still
    reflects the unbound slot (ok=False).
    """
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    daemon._last_pushed_snapshot = None
    host_cache.add_paired_device("codex", PairedDevice("dev-reboot", "10.0.0.5:80"))

    async def fake_fetch_health(host, client_id, client):
        return codex_daemon.HEALTH_AUTH_REJECTED

    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    pushed = False

    async def fake_push_to_all(snapshot, devices, client_id, client):
        nonlocal pushed
        pushed = True
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    from burnscope_client import pusher as pusher_mod

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return []
    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)
    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        # Let several cycles run.
        for _ in range(40):
            state = host_cache.read_push_state("codex", device_id="dev-reboot")
            if state is not None and state.get("ok") is False:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("per-device ok=False never recorded for 401 device")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert pushed is False
    # Pairing is preserved — a 401 on /health is not an eviction signal.
    paired = {d.device_id for d in host_cache.load_paired_devices("codex")}
    assert paired == {"dev-reboot"}


async def test_health_loop_401_rebind_drops_device_on_summary_conflict(monkeypatch):
    """Guard #3 (issue #66): a slot owned by a *different* client_id is a
    genuine ownership conflict, not a reclaimable empty slot. The re-bind
    push routes through /summary, whose TOFU only binds an empty slot; an
    owned slot returns 401 and the existing auth path drops the pairing
    rather than overwriting it.
    """
    from burnscope_client.discovery import DiscoveredDevice

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-owned", "10.0.0.5:80"))
    daemon._last_pushed_snapshot = await _rebind_snapshot()

    async def fake_fetch_health(host, client_id, client):
        return codex_daemon.HEALTH_AUTH_REJECTED

    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    async def fake_push_to_all(snapshot, devices, client_id, client):
        # The slot is owned by someone else → /summary 401.
        return {d.device_id: PushResult(d.device_id, False, "auth") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    from burnscope_client import pusher as pusher_mod

    # mDNS confirms the device at its host, so the re-bind push proceeds and
    # gets the 401 that drops it.
    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return [DiscoveredDevice("dev-owned", "10.0.0.5:80", True, False)]
    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)
    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        for _ in range(200):
            if not host_cache.load_paired_devices("codex"):
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("genuine-conflict device was never dropped")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert host_cache.load_paired_devices("codex") == []


async def test_health_loop_401_does_not_rebind_at_unverified_stale_host(monkeypatch):
    """Codex P2: a health 401 can be a *different* display answering at a
    stale cached host (DHCP reassigned the IP). Re-binding blind would
    TOFU-claim the wrong device. When mDNS can't confirm the device_id is
    still at the cached host, the loop must NOT push /summary there.
    """
    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-A", "10.0.0.5:80"))
    daemon._last_pushed_snapshot = await _rebind_snapshot()

    async def fake_fetch_health(host, client_id, client):
        return codex_daemon.HEALTH_AUTH_REJECTED

    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    pushed = False

    async def fake_push_to_all(snapshot, devices, client_id, client):
        nonlocal pushed
        pushed = True
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    from burnscope_client import pusher as pusher_mod

    # mDNS does NOT see dev-A (offline / moved); the cached host belongs to
    # someone else now.
    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return []
    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)
    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        # Let several cycles run so a rogue push would have fired.
        for _ in range(40):
            await asyncio.sleep(0.005)
            if pushed:
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # No /summary push at the unverified stale host → no wrong-device bind.
    assert pushed is False
    # Pairing preserved and host untouched.
    assert host_cache.load_paired_devices("codex") == [
        PairedDevice("dev-A", "10.0.0.5:80")
    ]


async def test_health_loop_401_rebinds_at_mdns_corrected_host(monkeypatch):
    """When mDNS locates the 401 device at a *new* host, the re-bind must
    target that mDNS-confirmed host (and refresh the cache) — never the
    stale cached host.
    """
    from burnscope_client.discovery import DiscoveredDevice

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-A", "10.0.0.5:80"))
    daemon._last_pushed_snapshot = await _rebind_snapshot()

    async def fake_fetch_health(host, client_id, client):
        return codex_daemon.HEALTH_AUTH_REJECTED

    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    pushed_hosts: list[str] = []

    async def fake_push_to_all(snapshot, devices, client_id, client):
        pushed_hosts.extend(d.host for d in devices)
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(codex_daemon, "push_to_all", fake_push_to_all)

    from burnscope_client import pusher as pusher_mod

    # dev-A actually lives at 10.0.0.9 now.
    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return [DiscoveredDevice("dev-A", "10.0.0.9:80", True, False)]
    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)
    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        for _ in range(200):
            if pushed_hosts:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("health loop never re-pushed at the corrected host")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Re-bind targeted the mDNS host, never the stale one.
    assert pushed_hosts[0] == "10.0.0.9:80"
    assert "10.0.0.5:80" not in pushed_hosts
    # Cache was refreshed to the confirmed host.
    assert host_cache.load_paired_devices("codex") == [
        PairedDevice("dev-A", "10.0.0.9:80")
    ]


async def test_reconcile_health_retry_401_does_not_crash(monkeypatch):
    """Compound case: a transport-failed device is rediscovered at a new
    host via mDNS, but the retry there returns 401 (slot unbound at the new
    host too). The retry must not be mistaken for a healthy body — passing
    the auth sentinel into the divergence comparison would crash the health
    loop. It's recorded as a miss and re-bound on the next cycle's initial
    probe (issue #66).
    """
    from burnscope_client.discovery import DiscoveredDevice

    daemon = CodexDaemon()
    daemon._client_id = "u@example.com"
    host_cache.add_paired_device("codex", PairedDevice("dev-moved", "10.0.0.5:80"))

    async def fake_fetch_health(host, client_id, client):
        if host == "10.0.0.5:80":
            return None  # initial probe: transport failure
        return codex_daemon.HEALTH_AUTH_REJECTED  # retry at new host: 401

    monkeypatch.setattr(codex_daemon, "fetch_health", fake_fetch_health)
    monkeypatch.setattr(codex_daemon, "HEALTH_INTERVAL_S", 0.005)

    async def fake_discover_all(timeout=4.0, agent=None, zc=None):
        return [DiscoveredDevice("dev-moved", "10.0.0.9:80", True, False)]
    monkeypatch.setattr(codex_daemon, "discover_all", fake_discover_all)

    from burnscope_client import pusher as pusher_mod
    monkeypatch.setattr(pusher_mod, "discover_all", fake_discover_all)

    task = asyncio.create_task(daemon._health_loop())
    try:
        for _ in range(400):
            cached = host_cache.load_paired_devices("codex")
            if cached and cached[0].host == "10.0.0.9:80":
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("health loop never refreshed cached host")
    finally:
        task.cancel()
        # A clean cancel proves the loop survived the auth sentinel without
        # raising (a crash would surface here as a non-CancelledError).
        with pytest.raises(asyncio.CancelledError):
            await task

    # Host was refreshed; pairing preserved for the next-cycle re-bind.
    assert host_cache.load_paired_devices("codex") == [
        PairedDevice("dev-moved", "10.0.0.9:80")
    ]
