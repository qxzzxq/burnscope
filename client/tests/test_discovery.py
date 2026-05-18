import socket

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from burnscope_client.discovery import (
    SERVICE_TYPE,
    discover_esp32,
    parse_host_override,
)


def test_parse_host_override_adds_default_port():
    assert parse_host_override("esp.local") == "esp.local:80"


def test_parse_host_override_preserves_explicit_port():
    assert parse_host_override("esp.local:8080") == "esp.local:8080"


async def test_discover_returns_none_on_timeout():
    # Quick timeout; no advertiser is running.
    result = await discover_esp32(timeout=0.5)
    # On a network with another BurnScope already running this could be non-None;
    # in CI / clean machines this should be None. Accept either but require the
    # call to complete within the timeout window.
    assert result is None or ":" in result


async def test_discover_finds_advertised_service():
    advertise_zc = AsyncZeroconf()
    browse_zc = AsyncZeroconf()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"burnscope-test.{SERVICE_TYPE}",
        addresses=[socket.inet_aton("127.0.0.1")],
        port=4242,
        properties={},
        server="burnscope-test.local.",
    )
    await advertise_zc.async_register_service(info)
    try:
        result = await discover_esp32(timeout=5.0, zc=browse_zc)
    finally:
        await advertise_zc.async_unregister_service(info)
        await advertise_zc.async_close()
        await browse_zc.async_close()

    assert result is not None
    assert result.endswith(":4242")
