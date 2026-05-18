import time
from pathlib import Path

import pytest

from burnscope_client.watcher import JsonlActivityWatcher


@pytest.fixture
def watcher(tmp_path: Path):
    w = JsonlActivityWatcher(tmp_path, poll_interval=0.1)
    w.start()
    try:
        yield w
    finally:
        w.stop()


def _wait_for(predicate, timeout=3.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_initial_last_change_is_none(tmp_path: Path):
    w = JsonlActivityWatcher(tmp_path)
    assert w.last_change_ts is None


def test_jsonl_modification_updates_last_change(watcher, tmp_path: Path):
    target = tmp_path / "session.jsonl"
    target.write_text("{}\n")

    assert _wait_for(lambda: watcher.last_change_ts is not None), \
        "watcher never observed jsonl write"
    first = watcher.last_change_ts

    time.sleep(0.2)
    with target.open("a") as f:
        f.write('{"more": 1}\n')

    assert _wait_for(lambda: watcher.last_change_ts and watcher.last_change_ts > first)


def test_non_jsonl_files_are_ignored(watcher, tmp_path: Path):
    (tmp_path / "notes.txt").write_text("ignored")
    assert not _wait_for(lambda: watcher.last_change_ts is not None, timeout=0.6)


def test_nested_jsonl_modifications_detected(watcher, tmp_path: Path):
    nested = tmp_path / "project-a"
    nested.mkdir()
    (nested / "session.jsonl").write_text("{}\n")
    assert _wait_for(lambda: watcher.last_change_ts is not None)


def test_stop_is_idempotent(tmp_path: Path):
    w = JsonlActivityWatcher(tmp_path)
    w.start()
    w.stop()
    w.stop()  # should not raise
