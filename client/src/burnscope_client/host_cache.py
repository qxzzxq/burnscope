"""File-backed state shared between the two collectors.

Three files in `~/.burnscope/` (mode 0700):
  - `host` — cached ESP32 `host:port`, written after successful mDNS resolve,
    deleted on transport failure to force rediscovery on the next attempt.
  - `last-push.claude` — `{"ok": bool, "at": int}`, written by the detached
    push child; read by the next statusline fire to render ✓/✗/….
  - `last-push.codex` — same shape; consumed only by `burnscope status`
    (the daemon remembers its own state in memory).

All writes are atomic via `NamedTemporaryFile` in the same directory plus
`os.replace`, so concurrent statusline fires from multiple Claude windows
can't corrupt any file.

State directory is overridable via `BURNSCOPE_STATE_DIR` for tests.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

_HOST_FILENAME = "host"
_LAST_PUSH_PREFIX = "last-push."
_CLIENT_ID_PREFIX = "client-id."


def state_dir() -> Path:
    """Resolve the state dir (honors `BURNSCOPE_STATE_DIR`)."""
    override = os.environ.get("BURNSCOPE_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".burnscope"


def _ensure_dir() -> Path:
    d = state_dir()
    d.mkdir(mode=0o700, exist_ok=True, parents=True)
    return d


def _atomic_write(path: Path, data: str) -> None:
    d = _ensure_dir()
    # tempfile in the same directory guarantees os.replace is atomic.
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup; ignore failure (the temp file might already be gone).
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_host() -> str | None:
    """Return the cached host (`host:port`), or None if missing/empty."""
    path = state_dir() / _HOST_FILENAME
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    return raw or None


def store_host(host: str) -> None:
    """Atomically write the cached host."""
    _atomic_write(state_dir() / _HOST_FILENAME, host)


def invalidate_host() -> None:
    """Remove the host cache. Idempotent — no-op if absent."""
    path = state_dir() / _HOST_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def write_push_state(agent: str, ok: bool) -> None:
    """Write the per-agent last-push outcome atomically."""
    payload = json.dumps({"ok": bool(ok), "at": int(time.time())})
    _atomic_write(state_dir() / f"{_LAST_PUSH_PREFIX}{agent}", payload)


def read_client_id(agent: str) -> str | None:
    """Return the cached per-agent SHA-256 client_id, or None.

    Cached on first derive so we don't hit the OS keyring on every Claude
    statusline fire (each fire spawns a fresh process and would otherwise
    re-prompt for Keychain access on macOS).
    """
    path = state_dir() / f"{_CLIENT_ID_PREFIX}{agent}"
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
        log.warning("client-id.%s contained malformed hash; ignoring", agent)
        return None
    return raw


def write_client_id(agent: str, client_id: str) -> None:
    """Persist the derived client_id atomically."""
    _atomic_write(state_dir() / f"{_CLIENT_ID_PREFIX}{agent}", client_id)


def invalidate_client_id(agent: str) -> None:
    """Drop the cached client_id. Idempotent."""
    path = state_dir() / f"{_CLIENT_ID_PREFIX}{agent}"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def read_push_state(agent: str) -> dict | None:
    """Return the parsed last-push state, or None if missing/malformed."""
    path = state_dir() / f"{_LAST_PUSH_PREFIX}{agent}"
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.warning("last-push.%s contained invalid JSON", agent)
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
