"""Tail ~/.claude/projects/**/*.jsonl for activity.

The daemon doesn't need event semantics; it just needs to know whether the
agent has done anything recently, so this exposes a single `last_change_ts`
(wall-clock unix seconds) that the daemon polls.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers.polling import PollingObserver


class _JsonlHandler(FileSystemEventHandler):
    def __init__(self, watcher: "JsonlActivityWatcher") -> None:
        self._watcher = watcher

    def _maybe_record(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if not str(event.src_path).endswith(".jsonl"):
            return
        self._watcher._record_change()

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_record(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe_record(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._maybe_record(event)


class JsonlActivityWatcher:
    """Records the wall-clock time of the most recent *.jsonl change beneath
    `root`. Uses a polling observer for cross-platform/test determinism.
    """

    def __init__(self, root: Path, poll_interval: float = 1.0) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()
        self._last_change_ts: float | None = None
        self._observer = PollingObserver(timeout=poll_interval)
        self._observer.schedule(_JsonlHandler(self), str(self._root), recursive=True)
        self._started = False

    @property
    def last_change_ts(self) -> float | None:
        with self._lock:
            return self._last_change_ts

    def _record_change(self) -> None:
        with self._lock:
            self._last_change_ts = time.time()

    def start(self) -> None:
        if self._started:
            return
        self._root.mkdir(parents=True, exist_ok=True)
        self._observer.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._observer.stop()
        self._observer.join(timeout=5)
        self._started = False
