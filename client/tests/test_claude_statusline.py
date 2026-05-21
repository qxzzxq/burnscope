import io
import json
import sys
from unittest.mock import MagicMock

import pytest

from burnscope_client import claude_statusline, host_cache


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

    line = capsys.readouterr().out.strip()
    assert line.endswith("✓")


def test_foreground_uses_cross_indicator_after_failed_push(monkeypatch, capsys):
    host_cache.write_push_state("claude", ok=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload())))
    monkeypatch.setattr(claude_statusline.subprocess, "Popen", MagicMock())

    claude_statusline.main([])

    line = capsys.readouterr().out.strip()
    assert line.endswith("✗")


def test_foreground_no_rate_limits_renders_dashes_and_skips_push(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(present=False))))
    popen = MagicMock()
    monkeypatch.setattr(claude_statusline.subprocess, "Popen", popen)

    rc = claude_statusline.main([])
    assert rc == 0

    out = capsys.readouterr().out.strip()
    assert "—" in out
    popen.assert_not_called()


def test_foreground_garbage_stdin_still_renders(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    popen = MagicMock()
    monkeypatch.setattr(claude_statusline.subprocess, "Popen", popen)

    rc = claude_statusline.main([])
    assert rc == 0
    popen.assert_not_called()


def test_push_mode_writes_ok_on_successful_push(monkeypatch):
    snapshot_dict = {
        "agent": "claude",
        "captured_at": 1779050146,
        "sessions": [
            {"type": "current", "used_pct": 0.23, "resets_at": 1779066600},
            {"type": "weekly", "used_pct": 0.41, "resets_at": 1779156000},
        ],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(snapshot_dict)))
    monkeypatch.setattr(
        claude_statusline.identity, "claude_user_identifier", lambda: "uuid-xyz"
    )

    async def fake_push(snap, host, client_id, client):
        assert host == "esp.local:80"
        assert client_id == "uuid-xyz"  # passed through as plaintext
        assert snap.agent == "claude"

    monkeypatch.setattr(claude_statusline, "push", fake_push)
    monkeypatch.setattr(claude_statusline.host_cache, "load_host", lambda: "esp.local:80")

    rc = claude_statusline.main(["--push"])
    assert rc == 0
    state = host_cache.read_push_state("claude")
    assert state["ok"] is True
    # First successful derive should populate the cache.
    assert host_cache.read_client_id("claude") is not None


def test_push_mode_uses_cached_client_id_without_re_reading_claude_json(monkeypatch):
    snapshot_dict = {
        "agent": "claude",
        "captured_at": 1779050146,
        "sessions": [
            {"type": "current", "used_pct": 0.23, "resets_at": 1779066600},
        ],
    }
    cached = "f" * 64
    host_cache.write_client_id("claude", cached)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(snapshot_dict)))

    def boom():
        raise AssertionError(
            "claude_user_identifier should NOT be called when client_id is cached"
        )

    monkeypatch.setattr(claude_statusline.identity, "claude_user_identifier", boom)
    monkeypatch.setattr(
        claude_statusline.host_cache, "load_host", lambda: "esp.local:80"
    )

    received: list[str] = []

    async def fake_push(snap, host, client_id, client):
        received.append(client_id)

    monkeypatch.setattr(claude_statusline, "push", fake_push)

    rc = claude_statusline.main(["--push"])
    assert rc == 0
    assert received == [cached]


def test_push_mode_writes_fail_and_invalidates_host_on_push_error(monkeypatch):
    snapshot_dict = {
        "agent": "claude",
        "captured_at": 1779050146,
        "sessions": [
            {"type": "current", "used_pct": 0.23, "resets_at": 1779066600},
        ],
    }
    host_cache.store_host("esp.local:80")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(snapshot_dict)))
    monkeypatch.setattr(
        claude_statusline.identity, "claude_user_identifier", lambda: "uuid-xyz"
    )

    async def fake_push(*args, **kwargs):
        raise claude_statusline.PushError("transport failed")

    monkeypatch.setattr(claude_statusline, "push", fake_push)

    rc = claude_statusline.main(["--push"])
    assert rc == 1
    assert host_cache.load_host() is None
    assert host_cache.read_push_state("claude")["ok"] is False


def test_push_mode_preserves_host_cache_on_auth_error(monkeypatch):
    snapshot_dict = {
        "agent": "claude",
        "captured_at": 1779050146,
        "sessions": [
            {"type": "current", "used_pct": 0.23, "resets_at": 1779066600},
        ],
    }
    host_cache.store_host("esp.local:80")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(snapshot_dict)))
    monkeypatch.setattr(
        claude_statusline.identity, "claude_user_identifier", lambda: "uuid-xyz"
    )

    async def fake_push(*args, **kwargs):
        raise claude_statusline.PushAuthError("401")

    monkeypatch.setattr(claude_statusline, "push", fake_push)

    rc = claude_statusline.main(["--push"])
    assert rc == 1
    # Host cache must survive — auth issue isn't a reachability issue.
    assert host_cache.load_host() == "esp.local:80"
    assert host_cache.read_push_state("claude")["ok"] is False


def test_push_mode_writes_fail_when_identity_unavailable(monkeypatch):
    snapshot_dict = {
        "agent": "claude",
        "captured_at": 1779050146,
        "sessions": [
            {"type": "current", "used_pct": 0.23, "resets_at": 1779066600},
        ],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(snapshot_dict)))

    def boom():
        raise claude_statusline.identity.IdentityError("no credentials")

    monkeypatch.setattr(claude_statusline.identity, "claude_user_identifier", boom)

    rc = claude_statusline.main(["--push"])
    assert rc == 1
    assert host_cache.read_push_state("claude")["ok"] is False
