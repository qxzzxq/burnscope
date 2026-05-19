"""Integration: run the daemon against a real local HTTP server and a real
JSONL watcher. Upstream probes are stubbed via fake `Agent` subclasses
(we never hit the network) but the discovery / push / file-watch wiring
is exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest

from burnscope_client.agent import Agent
from burnscope_client.daemon import DaemonConfig, run
from burnscope_client.schema import AgentSnapshot, SessionSnapshot


class _StubAgent(Agent):
    """Returns a fixed snapshot and records each probe call's wall-clock."""

    def __init__(self, name: str, snapshot: AgentSnapshot) -> None:
        # `name` is a class attribute on real agents; tests need it per-instance.
        self.name = name  # type: ignore[misc]
        self._snapshot = snapshot
        self.calls: list[float] = []

    async def probe(self, client: httpx.AsyncClient) -> AgentSnapshot:
        self.calls.append(asyncio.get_running_loop().time())
        return self._snapshot

    @classmethod
    def load_credential(cls):  # pragma: no cover - never called in tests
        return None


class _Receiver:
    """Fake firmware: mirrors the real device's edge-trigger semantics.

    `stored` is the per-agent snapshot map that the daemon will read back
    over `/health` to decide whether the device is in sync. `reset_firmware`
    simulates an ESP32 reboot wiping the RAM-only store.
    """

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.health_hits: int = 0
        self.stored: dict[str, list[dict]] = {}

    def reset_firmware(self) -> None:
        self.stored = {}

    def make_handler(self):
        receiver = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **k):  # silence
                pass

            def do_POST(self) -> None:
                if self.path != "/summary":
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                receiver.bodies.append(body)
                receiver.stored[body["agent"]] = body["sessions"]
                self.send_response(204)
                self.end_headers()

            def do_GET(self) -> None:
                if self.path != "/health":
                    self.send_response(404)
                    self.end_headers()
                    return
                receiver.health_hits += 1
                payload = {
                    "firmware_version": "test",
                    "uptime_s": 0,
                    "free_heap_b": 0,
                    "agents": {
                        name: {
                            "seconds_since_last_push": 0,
                            "sessions": sessions,
                        }
                        for name, sessions in receiver.stored.items()
                    },
                }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return _Handler


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_esp():
    port = _free_port()
    receiver = _Receiver()
    server = ThreadingHTTPServer(("127.0.0.1", port), receiver.make_handler())
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield receiver, f"127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_daemon_probes_and_pushes_to_fake_esp(tmp_path: Path, fake_esp):
    receiver, host = fake_esp

    expected = AgentSnapshot(
        agent="claude",
        captured_at=1779050146,
        sessions=[
            SessionSnapshot("current", 0.03, 1779066600),
            SessionSnapshot("weekly", 0.09, 1779156000),
        ],
    )
    agent = _StubAgent("claude", expected)

    config = DaemonConfig(
        agents=[agent],
        esp32_host=host,
        claude_projects_dir=tmp_path,
        active_interval=0.05,
        idle_interval=0.05,
        active_window=10,
        tick=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run(config, stop_event=stop))

    for _ in range(60):
        if receiver.bodies:
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert receiver.bodies, "daemon never pushed to /summary"
    assert receiver.bodies[0] == expected.to_dict()


async def test_daemon_uses_active_interval_after_jsonl_change(tmp_path: Path, fake_esp):
    receiver, host = fake_esp

    snapshot = AgentSnapshot(
        agent="claude",
        captured_at=1,
        sessions=[SessionSnapshot("current", 0.0, 1)],
    )
    agent = _StubAgent("claude", snapshot)

    config = DaemonConfig(
        agents=[agent],
        esp32_host=host,
        claude_projects_dir=tmp_path,
        active_interval=0.1,
        idle_interval=10.0,    # would not fire during test if idle stays active
        active_window=5.0,
        tick=0.05,
        watcher_poll_interval=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run(config, stop_event=stop))

    # Touch a jsonl to switch the daemon into active mode.
    await asyncio.sleep(0.1)
    (tmp_path / "session.jsonl").write_text("{}\n")

    # Wait long enough that an idle-only schedule would not yield 3 probes
    # but an active schedule (0.1s) will.
    await asyncio.sleep(1.0)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert len(agent.calls) >= 3, f"expected active cadence, got {agent.calls}"


async def test_daemon_pushes_one_snapshot_per_agent(tmp_path: Path, fake_esp):
    receiver, host = fake_esp

    claude_snap = AgentSnapshot(
        agent="claude",
        captured_at=1,
        sessions=[SessionSnapshot("current", 0.1, 100)],
    )
    codex_snap = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[SessionSnapshot("primary", 0.2, 200)],
    )

    config = DaemonConfig(
        agents=[
            _StubAgent("claude", claude_snap),
            _StubAgent("codex", codex_snap),
        ],
        esp32_host=host,
        claude_projects_dir=tmp_path,
        active_interval=0.05,
        idle_interval=0.05,
        active_window=10,
        tick=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run(config, stop_event=stop))

    # Wait for at least one push from each agent.
    agents_seen: set[str] = set()
    for _ in range(80):
        agents_seen = {b["agent"] for b in receiver.bodies}
        if {"claude", "codex"}.issubset(agents_seen):
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert {"claude", "codex"}.issubset(agents_seen), (
        f"daemon never pushed both agents; saw {agents_seen}"
    )


async def test_daemon_skips_push_when_snapshot_unchanged(tmp_path: Path, fake_esp):
    """Identical-content probes must not re-POST /summary.

    Re-pushing the same payload was forcing the firmware to repaint and
    yanking the on-device agent rotation; the daemon now treats /summary
    as edge-triggered and lets /health be the heartbeat.
    """
    receiver, host = fake_esp

    snapshot = AgentSnapshot(
        agent="claude",
        captured_at=1,
        sessions=[SessionSnapshot("current", 0.42, 1779066600)],
    )

    class _StableAgent(Agent):
        name = "claude"

        def __init__(self) -> None:
            self.probes = 0

        async def probe(self, client: httpx.AsyncClient) -> AgentSnapshot:
            self.probes += 1
            # Bump captured_at so payload equality is exercised on content,
            # not object identity.
            return AgentSnapshot(
                agent=snapshot.agent,
                captured_at=snapshot.captured_at + self.probes,
                sessions=list(snapshot.sessions),
            )

        @classmethod
        def load_credential(cls):  # pragma: no cover
            return None

    agent = _StableAgent()
    config = DaemonConfig(
        agents=[agent],
        esp32_host=host,
        claude_projects_dir=tmp_path,
        active_interval=0.05,
        idle_interval=0.05,
        active_window=10,
        tick=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run(config, stop_event=stop))

    # Wait for the first push to land, then keep ticking.
    for _ in range(60):
        if receiver.bodies:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)  # many ticks worth — would push ~10x under the old code

    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert agent.probes >= 3, f"expected several probes, got {agent.probes}"
    assert len(receiver.bodies) == 1, (
        f"expected exactly one push for an unchanged snapshot, got {len(receiver.bodies)}"
    )
    assert receiver.health_hits >= 1, "expected /health heartbeats when no push needed"


async def test_daemon_pushes_again_when_snapshot_changes(tmp_path: Path, fake_esp):
    """A content change on the next probe must re-POST /summary."""
    receiver, host = fake_esp

    class _MutableAgent(Agent):
        name = "claude"

        def __init__(self) -> None:
            self.probes = 0

        async def probe(self, client: httpx.AsyncClient) -> AgentSnapshot:
            self.probes += 1
            return AgentSnapshot(
                agent="claude",
                captured_at=1000 + self.probes,
                sessions=[SessionSnapshot("current", 0.1 * self.probes, 1779066600)],
            )

        @classmethod
        def load_credential(cls):  # pragma: no cover
            return None

    agent = _MutableAgent()
    config = DaemonConfig(
        agents=[agent],
        esp32_host=host,
        claude_projects_dir=tmp_path,
        active_interval=0.05,
        idle_interval=0.05,
        active_window=10,
        tick=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run(config, stop_event=stop))

    for _ in range(80):
        if len(receiver.bodies) >= 3:
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert len(receiver.bodies) >= 3, (
        f"expected one push per content change, got {len(receiver.bodies)}"
    )
    used_pcts = [b["sessions"][0]["used_pct"] for b in receiver.bodies]
    assert used_pcts == sorted(used_pcts), (
        f"pushed payloads should reflect monotonically increasing probes; got {used_pcts}"
    )


async def test_daemon_repushes_after_firmware_loses_state(tmp_path: Path, fake_esp):
    """Simulated ESP32 restart wipes the firmware-side store; the daemon
    must notice via /health and re-POST without waiting for upstream change.
    """
    receiver, host = fake_esp

    base = AgentSnapshot(
        agent="claude",
        captured_at=1,
        sessions=[SessionSnapshot("current", 0.42, 1779066600)],
    )

    class _StableAgent(Agent):
        name = "claude"

        def __init__(self) -> None:
            self.probes = 0

        async def probe(self, client: httpx.AsyncClient) -> AgentSnapshot:
            self.probes += 1
            return AgentSnapshot(
                agent=base.agent,
                captured_at=base.captured_at + self.probes,
                sessions=list(base.sessions),
            )

        @classmethod
        def load_credential(cls):  # pragma: no cover
            return None

    agent = _StableAgent()
    config = DaemonConfig(
        agents=[agent],
        esp32_host=host,
        claude_projects_dir=tmp_path,
        active_interval=0.05,
        idle_interval=0.05,
        active_window=10,
        tick=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run(config, stop_event=stop))

    # Wait for the first push.
    for _ in range(60):
        if receiver.bodies:
            break
        await asyncio.sleep(0.05)
    initial_pushes = len(receiver.bodies)
    assert initial_pushes >= 1, "daemon never pushed at all"

    # A few quiet ticks: the daemon must not re-push while firmware still
    # has the same content stored.
    await asyncio.sleep(0.4)
    assert len(receiver.bodies) == initial_pushes, (
        f"unexpected re-push during quiet period: {receiver.bodies}"
    )

    # Simulate ESP32 restart — firmware loses its in-RAM snapshots.
    receiver.reset_firmware()

    # Daemon should detect via /health and re-POST.
    for _ in range(60):
        if len(receiver.bodies) > initial_pushes:
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert len(receiver.bodies) > initial_pushes, (
        "daemon should re-POST after firmware loses state"
    )
    assert (
        receiver.bodies[-1]["sessions"] == receiver.bodies[0]["sessions"]
    ), "the re-pushed payload should carry the same content"


async def test_daemon_isolates_probe_failures(tmp_path: Path, fake_esp):
    receiver, host = fake_esp

    class _FailingAgent(Agent):
        name = "claude"

        def __init__(self) -> None:
            pass

        async def probe(self, client: httpx.AsyncClient) -> AgentSnapshot:
            raise RuntimeError("boom")

        @classmethod
        def load_credential(cls):  # pragma: no cover
            return None

    good_snap = AgentSnapshot(
        agent="codex",
        captured_at=1,
        sessions=[SessionSnapshot("primary", 0.5, 999)],
    )

    config = DaemonConfig(
        agents=[_FailingAgent(), _StubAgent("codex", good_snap)],
        esp32_host=host,
        claude_projects_dir=tmp_path,
        active_interval=0.05,
        idle_interval=0.05,
        active_window=10,
        tick=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run(config, stop_event=stop))

    for _ in range(60):
        if any(b["agent"] == "codex" for b in receiver.bodies):
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(task, timeout=2)

    pushed = {b["agent"] for b in receiver.bodies}
    assert pushed == {"codex"}, f"failing agent should not push; got {pushed}"
