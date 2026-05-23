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

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_LAST_PUSH_PREFIX = "last-push."
_CLIENT_ID_PREFIX = "client-id."
_PAIRED_DEVICES_PREFIX = "paired-devices."
_LEGACY_HOST_FILENAME = "host"

_MAX_CLIENT_ID_LEN = 254  # RFC 5321 email cap; leaves headroom for userIDs.


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


def _atomic_write(path: Path, data: str) -> None:
    d = _ensure_dir()
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
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
        raw = path.read_text()
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
    """
    existing = load_paired_devices(agent)
    updated = [d for d in existing if d.device_id != device.device_id]
    updated.append(device)
    save_paired_devices(agent, updated)


def remove_paired_device(agent: str, device_id: str) -> None:
    """Drop the device with this `device_id`. No-op if not present."""
    existing = load_paired_devices(agent)
    filtered = [d for d in existing if d.device_id != device_id]
    if len(filtered) == len(existing):
        return
    save_paired_devices(agent, filtered)


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
    return state_dir() / f"{_LAST_PUSH_PREFIX}{agent}.{device_id}"


def write_push_state(agent: str, ok: bool, *, device_id: str | None = None) -> None:
    """Write the aggregate (`device_id=None`) or per-device push outcome."""
    payload = json.dumps({"ok": bool(ok), "at": int(time.time())})
    _atomic_write(_last_push_path(agent, device_id), payload)


def read_push_state(agent: str, *, device_id: str | None = None) -> dict | None:
    """Return the parsed last-push state, or None if missing/malformed."""
    path = _last_push_path(agent, device_id)
    try:
        raw = path.read_text()
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
        raw = path.read_text().strip()
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

    No state migration: each collector's next fire hits the empty
    paired-list branch, runs discovery, and re-claims the device via
    the existing per-agent TOFU bindings. Idempotent.
    """
    path = state_dir() / _LEGACY_HOST_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        return
    log.info("removed legacy single-host cache file %s", path)
