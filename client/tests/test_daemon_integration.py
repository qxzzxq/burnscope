"""Integration: run the daemon against a real local HTTP server and a real
JSONL watcher. The Anthropic probe is stubbed (we never hit the network) but
the discovery / push / file-watch wiring is exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from burnscope_client.claude import AgentSnapshot, SessionSnapshot
from burnscope_client.daemon import DaemonConfig, run


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

    async def fake_probe(token, client):
        return expected

    config = DaemonConfig(
        token="dummy",
        esp32_host=host,
        claude_projects_dir=tmp_path,
        active_interval=0.05,
        idle_interval=0.05,
        active_window=10,
        tick=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run(config, stop_event=stop, probe_fn=fake_probe))

    # Wait until the receiver has at least one body.
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
    probe_calls: list[float] = []

    async def fake_probe(token, client):
        probe_calls.append(asyncio.get_running_loop().time())
        return snapshot

    config = DaemonConfig(
        token="t",
        esp32_host=host,
        claude_projects_dir=tmp_path,
        active_interval=0.1,
        idle_interval=10.0,    # would not fire during test if idle stays active
        active_window=5.0,
        tick=0.05,
        watcher_poll_interval=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run(config, stop_event=stop, probe_fn=fake_probe))

    # Touch a jsonl to switch the daemon into active mode.
    await asyncio.sleep(0.1)
    (tmp_path / "session.jsonl").write_text("{}\n")

    # Wait long enough that an idle-only schedule would not yield 3 probes
    # but an active schedule (0.1s) will.
    await asyncio.sleep(1.0)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert len(probe_calls) >= 3, f"expected active cadence, got {probe_calls}"
