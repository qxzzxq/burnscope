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
    def __init__(self) -> None:
        self.bodies: list[dict] = []

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
                body = self.rfile.read(length)
                receiver.bodies.append(json.loads(body))
                self.send_response(204)
                self.end_headers()

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
            SessionSnapshot("5h", 0.03, 1779066600),
            SessionSnapshot("7d", 0.09, 1779156000),
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
        sessions=[SessionSnapshot("5h", 0.0, 1)],
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
        sessions=[SessionSnapshot("5h", 0.1, 100)],
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
