import json
import threading

import pytest

from burnscope_client import host_cache
from burnscope_client.host_cache import PairedDevice


@pytest.fixture(autouse=True)
def _state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))
    return tmp_path


# --------------------------------------------------------- paired devices

def test_load_paired_devices_returns_empty_when_missing():
    assert host_cache.load_paired_devices("claude") == []


def test_save_and_load_paired_devices_round_trip():
    devices = [
        PairedDevice("burnscope-a1", "10.0.0.5:80"),
        PairedDevice("burnscope-b2", "10.0.0.6:80"),
    ]
    host_cache.save_paired_devices("claude", devices)
    loaded = host_cache.load_paired_devices("claude")
    assert loaded == devices


def test_paired_devices_are_per_agent():
    host_cache.save_paired_devices("claude", [PairedDevice("a", "1.1.1.1:80")])
    host_cache.save_paired_devices("codex",  [PairedDevice("b", "2.2.2.2:80")])
    assert host_cache.load_paired_devices("claude") == [PairedDevice("a", "1.1.1.1:80")]
    assert host_cache.load_paired_devices("codex")  == [PairedDevice("b", "2.2.2.2:80")]


def test_add_paired_device_appends_and_dedupes(_state_dir):
    host_cache.add_paired_device("claude", PairedDevice("dev-1", "10.0.0.5:80"))
    host_cache.add_paired_device("claude", PairedDevice("dev-2", "10.0.0.6:80"))
    host_cache.add_paired_device("claude", PairedDevice("dev-1", "10.0.0.7:80"))  # IP changed
    devices = host_cache.load_paired_devices("claude")
    by_id = {d.device_id: d for d in devices}
    assert set(by_id) == {"dev-1", "dev-2"}
    assert by_id["dev-1"].host == "10.0.0.7:80"  # latest host wins


def test_remove_paired_device_is_idempotent():
    host_cache.remove_paired_device("claude", "missing")  # no file yet
    host_cache.add_paired_device("claude", PairedDevice("dev-1", "10.0.0.5:80"))
    host_cache.remove_paired_device("claude", "dev-1")
    host_cache.remove_paired_device("claude", "dev-1")  # second time — no-op
    assert host_cache.load_paired_devices("claude") == []


def test_load_paired_devices_rejects_garbage(_state_dir):
    (_state_dir / "paired-devices.claude.json").write_text("not json")
    assert host_cache.load_paired_devices("claude") == []
    (_state_dir / "paired-devices.claude.json").write_text(
        json.dumps([{"device_id": "good", "host": "1.1.1.1:80"},
                    {"device_id": "", "host": "x"},          # empty id — skipped
                    {"device_id": "no-host"},                # missing host — skipped
                    "not a dict"])                            # non-object — skipped
    )
    devices = host_cache.load_paired_devices("claude")
    assert devices == [PairedDevice("good", "1.1.1.1:80")]


def test_concurrent_add_does_not_corrupt(_state_dir):
    threads = [
        threading.Thread(
            target=host_cache.add_paired_device,
            args=("claude", PairedDevice(f"dev-{i}", f"10.0.0.{i}:80")),
        )
        for i in range(20)
    ]
    for t in threads: t.start()
    for t in threads: t.join()
    # flock serializes read-modify-write, so all 20 devices survive.
    devices = host_cache.load_paired_devices("claude")
    assert len(devices) == 20
    assert all(d.device_id.startswith("dev-") for d in devices)


def test_clear_paired_devices():
    host_cache.add_paired_device("claude", PairedDevice("dev-1", "10.0.0.5:80"))
    host_cache.clear_paired_devices("claude")
    host_cache.clear_paired_devices("claude")  # idempotent
    assert host_cache.load_paired_devices("claude") == []


# --------------------------------------------------------- last-push state

def test_aggregate_push_state_round_trip():
    host_cache.write_push_state("claude", ok=True)
    state = host_cache.read_push_state("claude")
    assert state is not None and state["ok"] is True
    assert isinstance(state["at"], int)


def test_per_device_push_state_round_trip():
    host_cache.write_push_state("claude", ok=False, device_id="burnscope-cafe")
    state = host_cache.read_push_state("claude", device_id="burnscope-cafe")
    assert state is not None and state["ok"] is False


def test_per_device_state_does_not_pollute_aggregate(_state_dir):
    host_cache.write_push_state("claude", ok=True)
    host_cache.write_push_state("claude", ok=False, device_id="burnscope-cafe")
    assert host_cache.read_push_state("claude")["ok"] is True
    assert host_cache.read_push_state("claude", device_id="burnscope-cafe")["ok"] is False


def test_push_state_is_per_agent():
    host_cache.write_push_state("claude", ok=True)
    host_cache.write_push_state("codex", ok=False)
    assert host_cache.read_push_state("claude")["ok"] is True
    assert host_cache.read_push_state("codex")["ok"] is False


def test_read_push_state_returns_none_when_missing():
    assert host_cache.read_push_state("claude") is None


def test_read_push_state_returns_none_for_invalid_json(_state_dir):
    (_state_dir / "last-push.claude").write_text("not json")
    assert host_cache.read_push_state("claude") is None


# --------------------------------------------------------- client_id cache

def test_client_id_round_trip_with_email():
    host_cache.write_client_id("claude", "you@example.com")
    assert host_cache.read_client_id("claude") == "you@example.com"


def test_client_id_is_per_agent():
    host_cache.write_client_id("claude", "you@example.com")
    host_cache.write_client_id("codex", "other@example.com")
    assert host_cache.read_client_id("claude") == "you@example.com"
    assert host_cache.read_client_id("codex") == "other@example.com"


def test_read_client_id_rejects_empty(_state_dir):
    (_state_dir / "client-id.claude").write_text("   \n")
    assert host_cache.read_client_id("claude") is None


def test_read_client_id_rejects_overlong(_state_dir):
    (_state_dir / "client-id.claude").write_text("x" * 500)
    assert host_cache.read_client_id("claude") is None


def test_read_client_id_rejects_control_characters(_state_dir):
    (_state_dir / "client-id.claude").write_text("inner\tcontrol")
    assert host_cache.read_client_id("claude") is None


def test_invalidate_client_id_is_idempotent():
    host_cache.invalidate_client_id("claude")
    host_cache.write_client_id("claude", "you@example.com")
    host_cache.invalidate_client_id("claude")
    assert host_cache.read_client_id("claude") is None


# --------------------------------------------------------- migration

def test_migrate_legacy_host_file_removes_file(_state_dir):
    legacy = _state_dir / "host"
    legacy.write_text("10.0.0.5:80\n")
    host_cache.migrate_legacy_host_file()
    assert not legacy.exists()


def test_migrate_legacy_host_file_is_idempotent(_state_dir):
    host_cache.migrate_legacy_host_file()  # nothing to do
    host_cache.migrate_legacy_host_file()  # still nothing
    assert not (_state_dir / "host").exists()


# --------------------------------------------------------- state dir

def test_state_dir_creates_directory_with_0700(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "burnscope"
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(target))
    host_cache.write_client_id("claude", "you@example.com")
    assert target.is_dir()
    assert (target.stat().st_mode & 0o777) == 0o700


# ------------------------------------------------------- device_id sanitization

def test_add_paired_device_rejects_unsafe_device_id(_state_dir):
    host_cache.add_paired_device("claude", PairedDevice("../../etc/passwd", "10.0.0.5:80"))
    assert host_cache.load_paired_devices("claude") == []


def test_load_paired_devices_skips_unsafe_device_id(_state_dir):
    (_state_dir / "paired-devices.claude.json").write_text(
        json.dumps([
            {"device_id": "burnscope-12ab", "host": "10.0.0.5:80"},
            {"device_id": "../../etc/passwd", "host": "10.0.0.6:80"},
        ]),
        encoding="utf-8",
    )
    devices = host_cache.load_paired_devices("claude")
    assert len(devices) == 1
    assert devices[0].device_id == "burnscope-12ab"


def test_add_paired_device_rejects_empty_device_id(_state_dir):
    host_cache.add_paired_device("claude", PairedDevice("", "10.0.0.5:80"))
    assert host_cache.load_paired_devices("claude") == []


# -------------------------------------------------- push-state cleanup (#22)

def test_unlink_push_state_removes_per_device_file(_state_dir):
    host_cache.write_push_state("claude", ok=False, device_id="burnscope-cafe")
    assert host_cache.read_push_state("claude", device_id="burnscope-cafe") is not None
    host_cache.unlink_push_state("claude", "burnscope-cafe")
    assert host_cache.read_push_state("claude", device_id="burnscope-cafe") is None


def test_unlink_push_state_is_idempotent(_state_dir):
    host_cache.unlink_push_state("claude", "nonexistent")


def test_remove_paired_device_cleans_up_push_state(_state_dir):
    host_cache.add_paired_device("claude", PairedDevice("dev-1", "10.0.0.5:80"))
    host_cache.write_push_state("claude", ok=True, device_id="dev-1")
    host_cache.remove_paired_device("claude", "dev-1")
    assert host_cache.read_push_state("claude", device_id="dev-1") is None


def test_clear_push_state_removes_all(_state_dir):
    host_cache.write_push_state("claude", ok=True)
    host_cache.write_push_state("claude", ok=False, device_id="dev-1")
    host_cache.write_push_state("claude", ok=True, device_id="dev-2")
    host_cache.clear_push_state("claude")
    assert host_cache.read_push_state("claude") is None
    assert host_cache.read_push_state("claude", device_id="dev-1") is None
    assert host_cache.read_push_state("claude", device_id="dev-2") is None


def test_clear_push_state_does_not_touch_other_agent(_state_dir):
    host_cache.write_push_state("claude", ok=True)
    host_cache.write_push_state("codex", ok=False)
    host_cache.clear_push_state("claude")
    assert host_cache.read_push_state("claude") is None
    assert host_cache.read_push_state("codex") is not None


def test_write_push_state_drops_unsafe_device_id_silently(_state_dir):
    # A malicious mDNS responder must not be able to crash the long-lived
    # codex daemon or the statusline child by emitting a device_id that
    # contains path separators.
    host_cache.write_push_state("claude", ok=True, device_id="../../etc/passwd")
    # No per-device file was written under the safe-id allowlist.
    matches = list(_state_dir.glob("last-push.claude.*"))
    assert matches == []


def test_read_push_state_returns_none_for_unsafe_device_id(_state_dir):
    assert host_cache.read_push_state("claude", device_id="../evil") is None


# ---------------------------------------------------- v1→v2 upgrade hint (#17)

def test_migrate_legacy_upgrade_hint(_state_dir):
    legacy = _state_dir / "host"
    legacy.write_text("10.0.0.5:80\n")
    host_cache.migrate_legacy_host_file()
    assert host_cache.read_upgrade_hint() is not None
    assert "v1" in host_cache.read_upgrade_hint()


def test_clear_upgrade_hint(_state_dir):
    (_state_dir / "v1-upgrade-hint").write_text("test hint")
    host_cache.clear_upgrade_hint()
    assert host_cache.read_upgrade_hint() is None


def test_read_upgrade_hint_returns_none_when_missing(_state_dir):
    assert host_cache.read_upgrade_hint() is None


# -------------------------------------------------------- encoding (#29)

def test_atomic_write_survives_non_ascii_client_id(_state_dir):
    host_cache.write_client_id("claude", "jürgen@example.com")
    assert host_cache.read_client_id("claude") == "jürgen@example.com"


def test_paired_devices_round_trip_with_non_ascii_host(_state_dir):
    # host field can contain non-ASCII if mDNS instance name leaks through
    devices = [PairedDevice("burnscope-12ab", "10.0.0.5:80")]
    host_cache.save_paired_devices("claude", devices)
    loaded = host_cache.load_paired_devices("claude")
    assert loaded == devices


# ====================================== M-5: unlink_push_state inside the lock

def test_remove_paired_device_unlinks_push_state_under_lock(_state_dir):
    """remove_paired_device must clean up the per-device push-state
    file *inside* the same flock that protects the paired-devices
    file. Otherwise a concurrent writer could leave an orphan visible
    to `burnscope status` (deep-review M-5).

    We don't directly observe locking here, but we do verify the
    invariant downstream of it: after remove, the per-device file is
    gone.
    """
    host_cache.add_paired_device("claude", PairedDevice("dev-x", "10.0.0.5:80"))
    host_cache.write_push_state("claude", ok=False, device_id="dev-x")
    assert host_cache.read_push_state("claude", device_id="dev-x") is not None

    host_cache.remove_paired_device("claude", "dev-x")

    assert host_cache.load_paired_devices("claude") == []
    assert host_cache.read_push_state("claude", device_id="dev-x") is None


# ==================================================== L-7: fsync atomic write

def test_atomic_write_calls_fsync(monkeypatch, _state_dir):
    """_atomic_write must fsync the temp file before os.replace so
    the renamed file is durable across a host power loss, not just
    a process crash (deep-review L-7).
    """
    fsync_calls: list[int] = []
    real_fsync = host_cache.os.fsync

    def tracking_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(host_cache.os, "fsync", tracking_fsync)
    host_cache.write_client_id("claude", "fsynced@example.com")

    assert fsync_calls, "fsync was never called inside _atomic_write"
    assert host_cache.read_client_id("claude") == "fsynced@example.com"


# ===================================== M-3 plumbing: bump/reset failure counter

def test_bump_push_failures_increments_persisted_counter(_state_dir):
    """The counter must survive write/read cycles so the short-lived
    statusline child can reason about its eviction policy across
    independent fires (deep-review M-3).
    """
    assert host_cache.bump_push_failures("claude", "dev-x") == 1
    assert host_cache.bump_push_failures("claude", "dev-x") == 2
    assert host_cache.bump_push_failures("claude", "dev-x") == 3

    state = host_cache.read_push_state("claude", device_id="dev-x")
    assert state["ok"] is False
    assert state["consecutive_failures"] == 3


def test_reset_push_failures_clears_counter_and_marks_ok(_state_dir):
    host_cache.bump_push_failures("claude", "dev-x")
    host_cache.bump_push_failures("claude", "dev-x")

    host_cache.reset_push_failures("claude", "dev-x")

    state = host_cache.read_push_state("claude", device_id="dev-x")
    assert state["ok"] is True
    assert state["consecutive_failures"] == 0


def test_bump_push_failures_rejects_unsafe_device_id(_state_dir):
    """Malicious mDNS responder can't escape the state dir."""
    assert host_cache.bump_push_failures("claude", "../etc/passwd") == 0
    # No state file created.
    assert host_cache.read_push_state("claude", device_id="dev-x") is None
