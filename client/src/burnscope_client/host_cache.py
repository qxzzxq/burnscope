"""File-backed state shared between the two collectors.

Files in `~/.burnscope/` (mode 0700):
  - `paired-devices.<agent>.json` — list of `{device_id, host}` entries
    representing displays whose `<agent>` NVS slot is bound to this
    laptop's identifier. One file per agent; the Claude collector never
    reads or writes the Codex file (and vice versa).
  - `last-push.<agent>` — `{"ok": bool, "at": int}`, the aggregate
    outcome of the most recent fan-out push. `ok` is true iff every
    paired device responded 204. Drives the ✓/✗ indicator on the next
    statusline fire.
  - `last-push.<agent>.<device_id>` — same shape, scoped to one device.
    Lets `burnscope status` surface which specific display is failing.
  - `client-id.<agent>` — plaintext per-agent identifier (email or
    userID), cached so the Claude statusline doesn't re-read
    `~/.claude.json` on every fire.

A v1 single-host `host` file is silently deleted on first access — the
auto-pair-on-first-run path will re-claim the device via TOFU.

All writes are atomic via `NamedTemporaryFile` + `os.replace` so
concurrent statusline fires can't corrupt any file. State directory is
overridable via `BURNSCOPE_STATE_DIR` for tests.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

_LAST_PUSH_PREFIX = "last-push."
_CLIENT_ID_PREFIX = "client-id."
_PAIRED_DEVICES_PREFIX = "paired-devices."
_LEGACY_HOST_FILENAME = "host"

_MAX_CLIENT_ID_LEN = 254  # RFC 5321 email cap; leaves headroom for userIDs.
_MAX_DEVICE_ID_LEN = 64
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_UPGRADE_HINT_FILENAME = "v1-upgrade-hint"


@dataclass(frozen=True)
class PairedDevice:
    """One ESP32 display claimed by this laptop for a given agent.

    `device_id` matches `DiscoveredDevice.device_id` — derived from
    the mDNS hostname (`info.server`, e.g. `burnscope-a1b2`), which
    the firmware seeds from the WiFi MAC. It is stable across IP
    changes and unique per unit, so it's safe as the dictionary key
    on disk. `host` is the current `host:port` the device was last
    seen at.
    """

    device_id: str
    host: str


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


def _safe_device_id(raw: str) -> str | None:
    """Validate `device_id` for filesystem safety.

    Returns the id unchanged if it passes the allowlist + length cap;
    returns None if it contains path separators, traversal, or other
    unsafe characters (malicious mDNS responder on the LAN).
    """
    if not raw or len(raw) > _MAX_DEVICE_ID_LEN:
        return None
    if not _DEVICE_ID_RE.match(raw):
        return None
    return raw


def _lockfile_path(agent: str) -> Path:
    return state_dir() / f"paired-devices.{agent}.lock"


@contextmanager
def _with_lock(agent: str) -> Iterator[None]:
    """Hold an exclusive flock on the per-agent paired-devices lockfile.

    Serializes read-modify-write sequences so concurrent statusline
    children cannot clobber each other's updates via last-writer-wins.
    """
    path = _lockfile_path(agent)
    _ensure_dir()
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def unlink_push_state(agent: str, device_id: str) -> None:
    """Delete the per-device last-push file. Idempotent."""
    safe_id = _safe_device_id(device_id)
    if safe_id is None:
        return
    path = _last_push_path(agent, safe_id)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def clear_push_state(agent: str) -> None:
    """Delete every last-push file for `agent` (aggregate + per-device)."""
    d = state_dir()
    prefix = f"{_LAST_PUSH_PREFIX}{agent}"
    for path in d.glob(f"{prefix}*"):
        try:
            path.unlink()
        except OSError:
            pass


def _atomic_write(path: Path, data: str) -> None:
    d = _ensure_dir()
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ----------------------------------------------------------- paired devices

def _paired_devices_path(agent: str) -> Path:
    return state_dir() / f"{_PAIRED_DEVICES_PREFIX}{agent}.json"


def load_paired_devices(agent: str) -> list[PairedDevice]:
    """Return this agent's paired devices, or `[]` if absent/malformed."""
    path = _paired_devices_path(agent)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.warning("paired-devices.%s contained invalid JSON; ignoring", agent)
        return []
    if not isinstance(parsed, list):
        return []
    out: list[PairedDevice] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        device_id = entry.get("device_id")
        host = entry.get("host")
        if not isinstance(device_id, str) or not isinstance(host, str):
            continue
        if not device_id or not host:
            continue
        if _safe_device_id(device_id) is None:
            log.warning(
                "paired-devices.%s: skipping unsafe device_id %r", agent, device_id
            )
            continue
        out.append(PairedDevice(device_id=device_id, host=host))
    return out


def save_paired_devices(agent: str, devices: list[PairedDevice]) -> None:
    """Atomically replace the paired-device list for `agent`."""
    payload = json.dumps([asdict(d) for d in devices])
    _atomic_write(_paired_devices_path(agent), payload)


def add_paired_device(agent: str, device: PairedDevice) -> None:
    """Insert or update one device in the agent's paired list.

    Dedupes by `device_id`. If the device is already known under the
    same identifier, its stored `host` is updated to reflect the latest
    IP / port we saw it at.

    Serialized via flock so concurrent statusline children cannot
    clobber each other's updates.
    """
    if _safe_device_id(device.device_id) is None:
        log.warning("add_paired_device: rejecting unsafe device_id %r", device.device_id)
        return
    with _with_lock(agent):
        existing = load_paired_devices(agent)
        updated = [d for d in existing if d.device_id != device.device_id]
        updated.append(device)
        save_paired_devices(agent, updated)


def remove_paired_device(agent: str, device_id: str) -> None:
    """Drop the device with this `device_id`. No-op if not present.

    Serialized via flock so concurrent 401-drop paths cannot race.
    Also unlinks the per-device `last-push.<agent>.<device_id>` file
    so orphan push-state files don't accumulate.
    """
    with _with_lock(agent):
        existing = load_paired_devices(agent)
        filtered = [d for d in existing if d.device_id != device_id]
        if len(filtered) == len(existing):
            return
        save_paired_devices(agent, filtered)
    unlink_push_state(agent, device_id)


def clear_paired_devices(agent: str) -> None:
    """Remove the agent's paired-device file entirely."""
    path = _paired_devices_path(agent)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ----------------------------------------------------------- last-push state

def _last_push_path(agent: str, device_id: str | None) -> Path:
    if device_id is None:
        return state_dir() / f"{_LAST_PUSH_PREFIX}{agent}"
    safe = _safe_device_id(device_id)
    if safe is None:
        raise ValueError(f"unsafe device_id for filesystem path: {device_id!r}")
    return state_dir() / f"{_LAST_PUSH_PREFIX}{agent}.{safe}"


def write_push_state(agent: str, ok: bool, *, device_id: str | None = None) -> None:
    """Write the aggregate (`device_id=None`) or per-device push outcome."""
    payload = json.dumps({"ok": bool(ok), "at": int(time.time())})
    _atomic_write(_last_push_path(agent, device_id), payload)


def read_push_state(agent: str, *, device_id: str | None = None) -> dict | None:
    """Return the parsed last-push state, or None if missing/malformed."""
    path = _last_push_path(agent, device_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.warning("%s contained invalid JSON", path.name)
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# ------------------------------------------------------------- client_id cache

def read_client_id(agent: str) -> str | None:
    """Return the cached per-agent identifier, or None if missing/invalid."""
    path = state_dir() / f"{_CLIENT_ID_PREFIX}{agent}"
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw or len(raw) > _MAX_CLIENT_ID_LEN or any(ord(c) < 0x20 for c in raw):
        log.warning("client-id.%s contained malformed identifier; ignoring", agent)
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


# ----------------------------------------------------------------- migration

def migrate_legacy_host_file() -> None:
    """Delete the v1 single-host `host` file if it still exists.

    Writes a one-shot upgrade hint so `burnscope status` can surface a
    recovery message: the v1→v2 client_id format change (hash → plaintext
    email) means every previously-paired device will 401 until the user
    factory-resets it. Idempotent.
    """
    path = state_dir() / _LEGACY_HOST_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        return
    log.info("removed legacy single-host cache file %s", path)
    hint_path = state_dir() / _UPGRADE_HINT_FILENAME
    hint_path.write_text(
        "v1→v2 upgrade: client_id format changed (hash → plaintext email). "
        "Previously-paired devices will 401 until factory-reset. "
        "Run `burnscope pair` after resetting each device.\n",
        encoding="utf-8",
    )


def read_upgrade_hint() -> str | None:
    """Return the v1→v2 upgrade hint if present, or None."""
    path = state_dir() / _UPGRADE_HINT_FILENAME
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def clear_upgrade_hint() -> None:
    """Delete the v1→v2 upgrade hint. Idempotent."""
    path = state_dir() / _UPGRADE_HINT_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        pass
