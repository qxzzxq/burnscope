import io
import json
from unittest.mock import MagicMock

import pytest

from burnscope_client import claude_statusline, host_cache
from burnscope_client.host_cache import PairedDevice
from burnscope_client.pusher import PushResult


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))
    return tmp_path


def _payload(five=23.5, seven=41.2, present=True):
    if not present:
        return {}
    return {
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": 1779066600},
            "seven_day": {"used_percentage": seven, "resets_at": 1779156000},
        }
    }


# ============================================================ foreground

def test_foreground_renders_with_pending_indicator(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload())))
    popen = MagicMock()
    popen_instance = MagicMock()
    popen_instance.stdin = MagicMock()
    popen.return_value = popen_instance
    monkeypatch.setattr(claude_statusline.subprocess, "Popen", popen)

    rc = claude_statusline.main([])
    assert rc == 0

    out = capsys.readouterr().out.strip()
    assert out == "5h 24% · 7d 41% …"
    popen.assert_called_once()
    assert "--push" in popen.call_args.args[0]
    written = popen_instance.stdin.write.call_args.args[0]
    snap = json.loads(written.decode())
    assert snap["agent"] == "claude"
    assert {s["type"] for s in snap["sessions"]} == {"current", "weekly"}


def test_foreground_uses_check_indicator_after_successful_push(monkeypatch, capsys):
    host_cache.write_push_state("claude", ok=True)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload())))
    monkeypatch.setattr(claude_statusline.subprocess, "Popen", MagicMock())

    claude_statusline.main([])
    assert capsys.readouterr().out.strip().endswith("✓")


def test_foreground_uses_cross_indicator_after_failed_push(monkeypatch, capsys):
    host_cache.write_push_state("claude", ok=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload())))
    monkeypatch.setattr(claude_statusline.subprocess, "Popen", MagicMock())

    claude_statusline.main([])
    assert capsys.readouterr().out.strip().endswith("✗")


def test_foreground_no_rate_limits_renders_dashes_and_skips_push(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(present=False))))
    popen = MagicMock()
    monkeypatch.setattr(claude_statusline.subprocess, "Popen", popen)

    rc = claude_statusline.main([])
    assert rc == 0
    assert "—" in capsys.readouterr().out
    popen.assert_not_called()


def test_foreground_garbage_stdin_still_renders(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    popen = MagicMock()
    monkeypatch.setattr(claude_statusline.subprocess, "Popen", popen)

    rc = claude_statusline.main([])
    assert rc == 0
    popen.assert_not_called()


# ================================================================== push

_SNAP = {
    "agent": "claude",
    "captured_at": 1779050146,
    "sessions": [
        {"type": "current", "used_pct": 0.23, "resets_at": 1779066600},
        {"type": "weekly",  "used_pct": 0.41, "resets_at": 1779156000},
    ],
}


def _stub_stdin(monkeypatch, payload=None):
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(payload if payload is not None else _SNAP))
    )


def _stub_identity(monkeypatch, value="me@example.com"):
    monkeypatch.setattr(
        claude_statusline.identity, "claude_user_identifier", lambda: value
    )


def _stub_push_to_all(monkeypatch, results):
    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {r.device_id: r for r in results}
    monkeypatch.setattr(claude_statusline, "push_to_all", fake_push_to_all)


def _stub_discover_none(monkeypatch):
    async def fake_discover_all(timeout=10.0, agent=None, zc=None):
        return []
    monkeypatch.setattr(claude_statusline, "discover_all", fake_discover_all)


def test_push_uses_cached_paired_list_without_discovery(monkeypatch):
    _stub_stdin(monkeypatch)
    _stub_identity(monkeypatch)
    host_cache.add_paired_device("claude", PairedDevice("dev-a", "10.0.0.5:80"))
    host_cache.add_paired_device("claude", PairedDevice("dev-b", "10.0.0.6:80"))

    def boom(*a, **kw):
        raise AssertionError("discover_all must not run when paired list is hot")
    monkeypatch.setattr(claude_statusline, "discover_all", boom)

    seen_devices = []

    async def fake_push_to_all(snapshot, devices, client_id, client):
        seen_devices.extend(devices)
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(claude_statusline, "push_to_all", fake_push_to_all)

    rc = claude_statusline.main(["--push"])
    assert rc == 0
    assert {d.device_id for d in seen_devices} == {"dev-a", "dev-b"}
    assert host_cache.read_push_state("claude")["ok"] is True
    assert host_cache.read_push_state("claude", device_id="dev-a")["ok"] is True


def test_push_auto_pairs_when_paired_list_empty(monkeypatch):
    """Empty list → discover_all + per-device claim, then fan-out."""
    _stub_stdin(monkeypatch)
    _stub_identity(monkeypatch)
    host_cache.write_client_id("claude", "me@example.com")

    from burnscope_client.discovery import DiscoveredDevice

    async def fake_discover_all(timeout=10.0, agent=None, zc=None):
        return [
            DiscoveredDevice("dev-free-1", "10.0.0.5:80", False, False),
            DiscoveredDevice("dev-free-2", "10.0.0.6:80", False, True),
        ]

    monkeypatch.setattr(claude_statusline, "discover_all", fake_discover_all)

    claim_calls = []

    async def fake_push(snap, host, client_id, client):
        claim_calls.append((host, client_id))

    monkeypatch.setattr(claude_statusline, "push", fake_push)

    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(claude_statusline, "push_to_all", fake_push_to_all)

    rc = claude_statusline.main(["--push"])
    assert rc == 0
    assert len(claim_calls) == 2
    paired = host_cache.load_paired_devices("claude")
    assert {d.device_id for d in paired} == {"dev-free-1", "dev-free-2"}


def test_push_auto_pair_silently_skips_401(monkeypatch):
    _stub_stdin(monkeypatch)
    _stub_identity(monkeypatch)

    from burnscope_client.discovery import DiscoveredDevice

    async def fake_discover_all(timeout=10.0, agent=None, zc=None):
        return [
            DiscoveredDevice("dev-ok",      "10.0.0.5:80", False, False),
            DiscoveredDevice("dev-stolen",  "10.0.0.6:80", False, False),
        ]

    monkeypatch.setattr(claude_statusline, "discover_all", fake_discover_all)

    async def fake_push(snap, host, client_id, client):
        if host == "10.0.0.6:80":
            raise claude_statusline.PushAuthError("401")

    monkeypatch.setattr(claude_statusline, "push", fake_push)

    async def fake_push_to_all(snapshot, devices, client_id, client):
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(claude_statusline, "push_to_all", fake_push_to_all)

    claude_statusline.main(["--push"])
    paired = {d.device_id for d in host_cache.load_paired_devices("claude")}
    assert paired == {"dev-ok"}


def test_push_silently_drops_device_on_401(monkeypatch):
    """A 401 from one device removes only that (agent, device) pair."""
    _stub_stdin(monkeypatch)
    _stub_identity(monkeypatch)
    host_cache.add_paired_device("claude", PairedDevice("dev-keep", "10.0.0.5:80"))
    host_cache.add_paired_device("claude", PairedDevice("dev-drop", "10.0.0.6:80"))

    _stub_push_to_all(
        monkeypatch,
        [
            PushResult("dev-keep", True,  "ok"),
            PushResult("dev-drop", False, "auth"),
        ],
    )

    claude_statusline.main(["--push"])

    remaining = {d.device_id for d in host_cache.load_paired_devices("claude")}
    assert remaining == {"dev-keep"}
    # Per-device push state is cleaned up together with the device removal (#22).
    assert host_cache.read_push_state("claude", device_id="dev-drop") is None
    assert host_cache.read_push_state("claude", device_id="dev-keep")["ok"] is True
    # Aggregate must reflect only still-paired devices (#23).
    assert host_cache.read_push_state("claude")["ok"] is True


def test_push_keeps_device_on_transport_error(monkeypatch):
    _stub_stdin(monkeypatch)
    _stub_identity(monkeypatch)
    host_cache.add_paired_device("claude", PairedDevice("dev-flaky", "10.0.0.5:80"))

    _stub_push_to_all(
        monkeypatch,
        [PushResult("dev-flaky", False, "transport")],
    )

    rc = claude_statusline.main(["--push"])
    assert rc == 1
    # Device must stay in the paired list — transport failure isn't ownership.
    paired = {d.device_id for d in host_cache.load_paired_devices("claude")}
    assert paired == {"dev-flaky"}
    assert host_cache.read_push_state("claude", device_id="dev-flaky")["ok"] is False
    assert host_cache.read_push_state("claude")["ok"] is False


def test_push_writes_aggregate_ok_only_when_every_device_succeeds(monkeypatch):
    _stub_stdin(monkeypatch)
    _stub_identity(monkeypatch)
    host_cache.add_paired_device("claude", PairedDevice("dev-a", "10.0.0.5:80"))
    host_cache.add_paired_device("claude", PairedDevice("dev-b", "10.0.0.6:80"))

    _stub_push_to_all(
        monkeypatch,
        [
            PushResult("dev-a", True,  "ok"),
            PushResult("dev-b", False, "transport"),
        ],
    )

    claude_statusline.main(["--push"])
    assert host_cache.read_push_state("claude")["ok"] is False


def test_push_uses_cached_client_id_without_re_reading_claude_json(monkeypatch):
    _stub_stdin(monkeypatch)
    cached = "you@example.com"
    host_cache.write_client_id("claude", cached)
    host_cache.add_paired_device("claude", PairedDevice("dev-a", "10.0.0.5:80"))

    def boom():
        raise AssertionError("identity.claude_user_identifier must not be called")
    monkeypatch.setattr(claude_statusline.identity, "claude_user_identifier", boom)

    seen_ids: list[str] = []

    async def fake_push_to_all(snapshot, devices, client_id, client):
        seen_ids.append(client_id)
        return {d.device_id: PushResult(d.device_id, True, "ok") for d in devices}

    monkeypatch.setattr(claude_statusline, "push_to_all", fake_push_to_all)

    claude_statusline.main(["--push"])
    assert seen_ids == [cached]


def test_push_returns_failure_when_no_devices_and_discovery_empty(monkeypatch):
    _stub_stdin(monkeypatch)
    _stub_identity(monkeypatch)
    _stub_discover_none(monkeypatch)

    rc = claude_statusline.main(["--push"])
    assert rc == 1
    assert host_cache.read_push_state("claude")["ok"] is False


def test_push_writes_fail_when_identity_unavailable(monkeypatch):
    _stub_stdin(monkeypatch)

    def boom():
        raise claude_statusline.identity.IdentityError("no credentials")
    monkeypatch.setattr(claude_statusline.identity, "claude_user_identifier", boom)

    rc = claude_statusline.main(["--push"])
    assert rc == 1
    assert host_cache.read_push_state("claude")["ok"] is False


def test_push_migrates_legacy_host_file(monkeypatch, _isolate_state):
    """First push after v2 upgrade deletes the legacy `host` file."""
    legacy = _isolate_state / "host"
    legacy.write_text("esp.local:80\n")
    _stub_stdin(monkeypatch)
    _stub_identity(monkeypatch)
    _stub_discover_none(monkeypatch)

    claude_statusline.main(["--push"])
    assert not legacy.exists()
