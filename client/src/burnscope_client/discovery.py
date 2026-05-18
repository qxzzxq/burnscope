"""mDNS discovery of the BurnScope ESP32 over `_burnscope._tcp.local.`."""

from __future__ import annotations

import asyncio
import logging
import socket

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

log = logging.getLogger(__name__)

SERVICE_TYPE = "_burnscope._tcp.local."


async def discover_esp32(
    timeout: float = 10.0,
    *,
    zc: AsyncZeroconf | None = None,
) -> str | None:
    """Browse mDNS for a BurnScope ESP32 and return `"host:port"` if found.

    Returns `None` on timeout. `zc` is injectable for tests.
    """
    own_zc = zc is None
    zc = zc or AsyncZeroconf()
    found: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def _on_change(zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added:
            return
        if found.done():
            return
        asyncio.create_task(_resolve(zc, service_type, name, found))

    browser = AsyncServiceBrowser(zc.zeroconf, SERVICE_TYPE, handlers=[_on_change])
    try:
        return await asyncio.wait_for(found, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        await browser.async_cancel()
        if own_zc:
            await zc.async_close()


async def _resolve(
    zc: AsyncZeroconf,
    service_type: str,
    name: str,
    future: asyncio.Future[str],
) -> None:
    info = AsyncServiceInfo(service_type, name)
    if not await info.async_request(zc.zeroconf, 3000):
        return
    addresses = info.parsed_addresses(IPVersion.V4Only)
    if not addresses or info.port is None:
        return
    if not future.done():
        future.set_result(f"{addresses[0]}:{info.port}")


def parse_host_override(value: str, default_port: int = 80) -> str:
    """Normalize a user-supplied `--esp32-host` into `host:port`."""
    if ":" in value:
        return value
    return f"{value}:{default_port}"


# Pulled out so tests can monkeypatch (zeroconf's actual hostname resolution
# is needed for service registration, but we keep this here for symmetry).
def local_hostname() -> str:
    return socket.gethostname()
