import json
import threading

import pytest

from burnscope_client import host_cache


@pytest.fixture(autouse=True)
def _state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))
    return tmp_path


def test_store_and_load_host_round_trip():
    host_cache.store_host("esp.local:80")
    assert host_cache.load_host() == "esp.local:80"


def test_load_host_returns_none_when_missing():
    assert host_cache.load_host() is None


def test_load_host_returns_none_when_empty(_state_dir):
    (_state_dir / "host").write_text("")
    assert host_cache.load_host() is None


def test_load_host_strips_trailing_whitespace(_state_dir):
    (_state_dir / "host").write_text("esp.local:80\n")
    assert host_cache.load_host() == "esp.local:80"


def test_invalidate_host_is_idempotent():
    host_cache.invalidate_host()  # no file yet — should not raise
    host_cache.store_host("esp.local:80")
    host_cache.invalidate_host()
    assert host_cache.load_host() is None


def test_concurrent_store_does_not_corrupt(_state_dir):
    values = [f"host-{i}:80" for i in range(20)]
    threads = [
        threading.Thread(target=host_cache.store_host, args=(v,)) for v in values
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    loaded = host_cache.load_host()
    assert loaded in values  # one of the writers won; file is intact


def test_push_state_round_trip():
    host_cache.write_push_state("claude", ok=True)
    state = host_cache.read_push_state("claude")
    assert state is not None
    assert state["ok"] is True
    assert isinstance(state["at"], int)


def test_push_state_is_per_agent(_state_dir):
    host_cache.write_push_state("claude", ok=True)
    host_cache.write_push_state("codex", ok=False)
    assert host_cache.read_push_state("claude")["ok"] is True
    assert host_cache.read_push_state("codex")["ok"] is False


def test_read_push_state_returns_none_when_missing():
    assert host_cache.read_push_state("claude") is None


def test_read_push_state_returns_none_for_invalid_json(_state_dir):
    (_state_dir / "last-push.claude").write_text("not json")
    assert host_cache.read_push_state("claude") is None


def test_client_id_round_trip():
    cid = "a" * 64
    host_cache.write_client_id("claude", cid)
    assert host_cache.read_client_id("claude") == cid


def test_client_id_is_per_agent():
    host_cache.write_client_id("claude", "a" * 64)
    host_cache.write_client_id("codex", "b" * 64)
    assert host_cache.read_client_id("claude") == "a" * 64
    assert host_cache.read_client_id("codex") == "b" * 64


def test_read_client_id_returns_none_when_missing():
    assert host_cache.read_client_id("claude") is None


def test_read_client_id_rejects_malformed_hash(_state_dir):
    (_state_dir / "client-id.claude").write_text("not-a-hash")
    assert host_cache.read_client_id("claude") is None


def test_read_client_id_rejects_wrong_length(_state_dir):
    (_state_dir / "client-id.claude").write_text("abcd")
    assert host_cache.read_client_id("claude") is None


def test_invalidate_client_id_is_idempotent():
    host_cache.invalidate_client_id("claude")  # missing — no-op
    host_cache.write_client_id("claude", "c" * 64)
    host_cache.invalidate_client_id("claude")
    assert host_cache.read_client_id("claude") is None


def test_state_dir_creates_directory_with_0700(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "burnscope"
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(target))
    host_cache.store_host("esp.local:80")
    assert target.is_dir()
    # 0o700 — bottom 9 bits should match rwx------ on POSIX.
    assert (target.stat().st_mode & 0o777) == 0o700
