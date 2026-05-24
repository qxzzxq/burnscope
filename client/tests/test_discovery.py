import logging
import socket

import pytest
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from burnscope_client.discovery import (
    SERVICE_TYPE,
    DiscoveredDevice,
    _txt_paired,
    discover_all,
    parse_host_override,
)


def test_parse_host_override_adds_default_port():
    assert parse_host_override("esp.local") == "esp.local:80"


def test_parse_host_override_preserves_explicit_port():
    assert parse_host_override("esp.local:8080") == "esp.local:8080"


# --------------------------------------------------------- _txt_paired

def test_txt_paired_recognises_literal_zero_and_one():
    assert _txt_paired({b"paired_claude": b"0"}, b"paired_claude") is False
    assert _txt_paired({b"paired_claude": b"1"}, b"paired_claude") is True


def test_txt_paired_missing_key_defaults_to_paired():
    # Legacy firmware without `paired_*` TXT items must be treated as
    # paired so we never auto-claim a device that can't surface its
    # slot state.
    assert _txt_paired({}, b"paired_claude") is True


def test_txt_paired_none_value_defaults_to_paired():
    assert _txt_paired({b"paired_claude": None}, b"paired_claude") is True


@pytest.mark.parametrize("raw", [b"", b"2", b"true", b"yes", b"01"])
def test_txt_paired_unknown_bytes_value_logs_and_treats_as_paired(raw, caplog):
    caplog.set_level(logging.WARNING, logger="burnscope_client.discovery")
    assert _txt_paired({b"paired_claude": raw}, b"paired_claude") is True
    assert any(
        "unrecognised value" in record.message and "paired_claude" in record.message
        for record in caplog.records
    ), caplog.records


@pytest.mark.parametrize("raw", ["", "2", "true"])
def test_txt_paired_unknown_str_value_logs_and_treats_as_paired(raw, caplog):
    caplog.set_level(logging.WARNING, logger="burnscope_client.discovery")
    assert _txt_paired({b"paired_claude": raw}, b"paired_claude") is True
    assert any(
        "unrecognised value" in record.message and "paired_claude" in record.message
        for record in caplog.records
    ), caplog.records


def test_txt_paired_str_zero_and_one_match():
    assert _txt_paired({b"paired_claude": "0"}, b"paired_claude") is False
    assert _txt_paired({b"paired_claude": "1"}, b"paired_claude") is True


def test_paired_for_dispatch_per_agent():
    d = DiscoveredDevice(
        device_id="burnscope-cafe",
        host="10.0.0.5:80",
        paired_claude=True,
        paired_codex=False,
    )
    assert d.paired_for("claude") is True
    assert d.paired_for("codex") is False
    # Unknown agents are conservatively reported as paired so the caller
    # never tries to TOFU-claim a slot it doesn't understand.
    assert d.paired_for("anthropic-future") is True


async def test_discover_returns_empty_on_timeout():
    # Quick timeout, isolated zeroconf instance — anything that happens
    # to be on the LAN is on a *different* zc and won't be picked up by
    # the isolated browse.
    isolated_zc = AsyncZeroconf()
    try:
        results = await discover_all(timeout=0.5, zc=isolated_zc)
    finally:
        await isolated_zc.async_close()
    # External BurnScopes on the LAN can still leak through (zeroconf
    # answers come from the network, not just from services registered
    # on the same AsyncZeroconf). Accept either: empty (clean LAN) or
    # well-formed devices.
    for d in results:
        assert d.host and ":" in d.host
        assert d.device_id


async def _register(zc: AsyncZeroconf, hostname: str, port: int, *, paired_claude: bool, paired_codex: bool, instance: str = "BurnScope"):
    """Register a fake BurnScope-like service.

    `hostname` is what becomes the stable device_id on the client side
    (the firmware derives it from the WiFi MAC). `instance` defaults to
    "BurnScope" to mirror the real firmware, where every device sets the
    same instance name and Bonjour disambiguates collisions by appending
    -2/-3/... — making the instance name unstable.
    """
    properties = {
        b"version": b"test",
        b"paired_claude": b"1" if paired_claude else b"0",
        b"paired_codex":  b"1" if paired_codex else b"0",
    }
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{instance}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton("127.0.0.1")],
        port=port,
        properties=properties,
        server=f"{hostname}.local.",
    )
    await zc.async_register_service(info)
    return info


async def test_discover_all_returns_every_advertised_device():
    advertise_zc = AsyncZeroconf()
    browse_zc = AsyncZeroconf()
    info_a = await _register(
        advertise_zc, "burnscope-a1a1", 4242,
        paired_claude=False, paired_codex=False, instance="BurnScope-X",
    )
    info_b = await _register(
        advertise_zc, "burnscope-b2b2", 4243,
        paired_claude=True, paired_codex=False, instance="BurnScope-Y",
    )
    try:
        devices = await discover_all(timeout=2.5, zc=browse_zc)
    finally:
        await advertise_zc.async_unregister_service(info_a)
        await advertise_zc.async_unregister_service(info_b)
        await advertise_zc.async_close()
        await browse_zc.async_close()

    by_id = {d.device_id: d for d in devices}
    assert "burnscope-a1a1" in by_id
    assert "burnscope-b2b2" in by_id
    assert by_id["burnscope-a1a1"].paired_claude is False
    assert by_id["burnscope-a1a1"].paired_codex is False
    assert by_id["burnscope-b2b2"].paired_claude is True


async def test_discover_all_filters_by_agent():
    advertise_zc = AsyncZeroconf()
    browse_zc = AsyncZeroconf()
    info_free = await _register(
        advertise_zc, "burnscope-free", 4244,
        paired_claude=False, paired_codex=False, instance="BurnScope-F",
    )
    info_taken = await _register(
        advertise_zc, "burnscope-takn", 4245,
        paired_claude=True, paired_codex=False, instance="BurnScope-T",
    )
    try:
        claude_devices = await discover_all(timeout=2.5, agent="claude", zc=browse_zc)
    finally:
        await advertise_zc.async_unregister_service(info_free)
        await advertise_zc.async_unregister_service(info_taken)
        await advertise_zc.async_close()
        await browse_zc.async_close()

    ids = {d.device_id for d in claude_devices}
    assert "burnscope-free" in ids
    # burnscope-takn has paired_claude=1 in TXT so it must be filtered
    # out of a claude-targeted browse — that's the whole point of the
    # multi-device redesign.
    assert "burnscope-takn" not in ids


async def test_discover_all_uses_hostname_when_instance_name_collides():
    """Two real BurnScopes ship with the same hardcoded instance name
    "BurnScope"; Bonjour disambiguates by appending "-2" to whichever
    responder it heard from second. That suffix assignment is
    order-dependent, so the instance name is NOT a stable device_id.
    The hostname (derived from the WiFi MAC) is. Verify discover_all
    picks the hostname even when instance names collide."""
    advertise_zc = AsyncZeroconf()
    browse_zc = AsyncZeroconf()
    # Both responders advertise the same instance name "BurnScope".
    # zeroconf on the advertiser side will internally rename one of
    # them; what we care about is that the CLIENT keys by hostname.
    info_a = await _register(
        advertise_zc, "burnscope-aaaa", 4250,
        paired_claude=False, paired_codex=False, instance="BurnScope",
    )
    # allow_name_change mirrors real Bonjour: the second responder gets
    # its instance renamed ("BurnScope" → "BurnScope-2") rather than
    # rejected. That's exactly the collision we're hardening against.
    info_b = ServiceInfo(
        SERVICE_TYPE,
        f"BurnScope.{SERVICE_TYPE}",
        addresses=[socket.inet_aton("127.0.0.1")],
        port=4251,
        properties={
            b"version": b"test",
            b"paired_claude": b"0",
            b"paired_codex": b"0",
        },
        server="burnscope-bbbb.local.",
    )
    await advertise_zc.async_register_service(info_b, allow_name_change=True)
    try:
        devices = await discover_all(timeout=2.5, zc=browse_zc)
    finally:
        await advertise_zc.async_unregister_service(info_a)
        await advertise_zc.async_unregister_service(info_b)
        await advertise_zc.async_close()
        await browse_zc.async_close()

    ids = {d.device_id for d in devices}
    # Both unique hostnames must round-trip even though the instance
    # names collided. A regression here means we'd silently re-key
    # devices between discovery runs.
    assert "burnscope-aaaa" in ids
    assert "burnscope-bbbb" in ids


async def test_discover_all_treats_legacy_firmware_as_paired():
    """A device without `paired_*` TXT items must be treated as paired."""
    advertise_zc = AsyncZeroconf()
    browse_zc = AsyncZeroconf()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"burnscope-old1.{SERVICE_TYPE}",
        addresses=[socket.inet_aton("127.0.0.1")],
        port=4246,
        properties={b"version": b"0.1.0"},  # legacy: no paired_* keys
        server="burnscope-old1.local.",
    )
    await advertise_zc.async_register_service(info)
    try:
        all_devices = await discover_all(timeout=2.5, zc=browse_zc)
        free_for_claude = await discover_all(timeout=2.5, agent="claude", zc=browse_zc)
    finally:
        await advertise_zc.async_unregister_service(info)
        await advertise_zc.async_close()
        await browse_zc.async_close()

    legacy = [d for d in all_devices if d.device_id == "burnscope-old1"]
    assert legacy and legacy[0].paired_claude is True
    assert legacy[0].paired_codex is True
    # An unclaimed legacy device must not appear in a per-agent free-slot
    # browse — the v2 client would otherwise auto-pair an older firmware
    # that doesn't know how to surface its slot state.
    assert all(d.device_id != "burnscope-old1" for d in free_for_claude)
