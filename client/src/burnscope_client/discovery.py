"""mDNS discovery of BurnScope ESP32 displays over `_burnscope._tcp.local.`.

`discover_all()` browses the full timeout window and returns every device
it sees on the LAN. Each result carries the per-agent `paired_*` hint
parsed from the firmware's TXT records — when `agent` is passed, the
list is filtered to devices whose slot for that agent is still free.

Legacy firmware (no `paired_*` TXT items) is conservatively treated as
"paired" so the client never auto-claims an older device that doesn't
know how to advertise its state.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

log = logging.getLogger(__name__)

SERVICE_TYPE = "_burnscope._tcp.local."

# Sentinel for legacy firmware that doesn't publish per-agent TXT items.
# Treating absent-TXT as paired=True means a v2 client never auto-claims
# an older device by accident; the user must factory-reset it first.
_LEGACY_PAIRED_DEFAULT = True


@dataclass(frozen=True)
class DiscoveredDevice:
    """One BurnScope display visible on the LAN.

    `device_id` is derived from the mDNS hostname (`info.server`,
    e.g. `burnscope-a1b2`). Because the firmware seeds the hostname
    from the WiFi MAC, the id is stable across IP changes and per-unit
    unique, so it is safe to use as a dictionary key on the client
    side. The mDNS *instance name* (the human-facing "BurnScope" label)
    is only used as a fallback when the SRV record is missing.
    """

    device_id: str
    host: str
    paired_claude: bool
    paired_codex: bool

    def paired_for(self, agent: str) -> bool:
        if agent == "claude":
            return self.paired_claude
        if agent == "codex":
            return self.paired_codex
        return True


async def discover_all(
    timeout: float = 10.0,
    *,
    agent: str | None = None,
    zc: AsyncZeroconf | None = None,
) -> list[DiscoveredDevice]:
    """Browse for the full `timeout`; return every BurnScope device seen.

    When `agent` is `"claude"` or `"codex"`, the result is filtered to
    devices where that slot is still free (`paired_<agent>=False`).
    """
    own_zc = zc is None
    zc = zc or AsyncZeroconf()
    devices: dict[str, DiscoveredDevice] = {}
    resolve_tasks: set[asyncio.Task] = set()

    log.debug("mDNS browse start: %s (timeout=%.1fs)", SERVICE_TYPE, timeout)

    def _on_change(zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added:
            return
        task = asyncio.create_task(_resolve(zc, service_type, name, devices))
        resolve_tasks.add(task)
        task.add_done_callback(resolve_tasks.discard)

    browser = AsyncServiceBrowser(zc.zeroconf, SERVICE_TYPE, handlers=[_on_change])
    try:
        await asyncio.sleep(timeout)
    finally:
        await browser.async_cancel()
        if resolve_tasks:
            await asyncio.gather(*resolve_tasks, return_exceptions=True)
        if own_zc:
            await zc.async_close()

    results = list(devices.values())
    log.debug("mDNS browse resolved %d device(s)", len(results))
    if agent is None:
        return results
    return [d for d in results if not d.paired_for(agent)]


async def _resolve(
    zc: AsyncZeroconf,
    service_type: str,
    name: str,
    sink: dict[str, DiscoveredDevice],
) -> None:
    info = AsyncServiceInfo(service_type, name)
    if not await info.async_request(zc.zeroconf, 3000):
        return
    addresses = info.parsed_addresses(IPVersion.V4Only)
    if not addresses or info.port is None:
        return

    # Prefer the mDNS hostname (`burnscope-<mac4>.local.`) as the device
    # identifier — the firmware derives it from the WiFi MAC so it is
    # genuinely unique per device. The mDNS *instance name* would be a
    # poor choice: every BurnScope firmware sets it to the literal string
    # "BurnScope", and Bonjour disambiguates collisions by appending
    # "-2", "-3", … to whichever responder it heard from second. That
    # ordering is unstable — the same physical device can appear as
    # "BurnScope" one run and "BurnScope-2" the next — so anything we
    # persisted under the instance name would silently re-key.
    device_id = _device_id_from(info, name, service_type)
    if not device_id:
        return

    paired_claude = _txt_paired(info.properties, b"paired_claude")
    paired_codex = _txt_paired(info.properties, b"paired_codex")

    device = DiscoveredDevice(
        device_id=device_id,
        host=f"{addresses[0]}:{info.port}",
        paired_claude=paired_claude,
        paired_codex=paired_codex,
    )
    sink[device_id] = device


def _device_id_from(info: AsyncServiceInfo, name: str, service_type: str) -> str:
    """Stable per-device identifier.

    Preferred: the mDNS hostname (`info.server`), with the trailing
    `.local.` stripped — uniquely derived from the device's MAC.
    Fallback (only when the SRV record is somehow missing): the
    instance-name portion of the mDNS name, which can collide.
    """
    server = info.server
    if server:
        host = server.rstrip(".")
        if host.endswith(".local"):
            host = host[: -len(".local")]
        if host:
            return host
    return _instance_name(name, service_type)


def _instance_name(name: str, service_type: str) -> str:
    """Strip the trailing `.<service-type>` to recover the instance name."""
    suffix = "." + service_type
    if name.endswith(suffix):
        return name[: -len(suffix)]
    return name.rstrip(".")


def _txt_paired(properties: dict, key: bytes) -> bool:
    """Read a `paired_<agent>` TXT entry.

    zeroconf delivers TXT properties as a `dict[bytes, bytes | None]`.
    Anything other than the literal byte `0` (or string `"0"`) is treated
    as paired — including a missing key, since we don't know what an older
    firmware's slot state is and shouldn't try to claim it.
    """
    if key not in properties:
        return _LEGACY_PAIRED_DEFAULT
    raw = properties[key]
    if raw is None:
        return _LEGACY_PAIRED_DEFAULT
    if isinstance(raw, bytes):
        return raw != b"0"
    if isinstance(raw, str):
        return raw != "0"
    return _LEGACY_PAIRED_DEFAULT


def parse_host_override(value: str, default_port: int = 80) -> str:
    """Normalize a user-supplied `--esp32-host` into `host:port`."""
    if ":" in value:
        return value
    return f"{value}:{default_port}"


def local_hostname() -> str:
    """Wrapper around `socket.gethostname()` for test monkeypatching."""
    return socket.gethostname()
