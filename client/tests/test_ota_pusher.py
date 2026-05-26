"""Tests for the OTA push helper.

Mirrors test_pusher.py's shape (respx-mocked httpx) since both
helpers do the same thing — POST to the ESP32 with the
`X-BurnScope-Client-Id` header — just at a different endpoint.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from burnscope_client.ota_pusher import (
    OTA_CONTENT_TYPE,
    push_ota,
)
from burnscope_client.pusher import (
    CLIENT_ID_HEADER,
    PushAuthError,
    PushError,
)

CLIENT_ID = "user@example.com"
IMAGE = b"\xe9" + b"\x00" * 4095  # 4 KiB; real OTA images start with 0xE9.


@respx.mock
async def test_push_ota_posts_with_octet_stream_and_client_id():
    """The /ota endpoint expects an octet-stream body and the same
    client-id header as /summary. Verify both are set verbatim."""
    captured: dict = {}

    def assert_request(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(202, json={"status": "flashing", "bytes": len(IMAGE)})

    respx.post("http://esp.local/ota").mock(side_effect=assert_request)

    async with httpx.AsyncClient() as client:
        result = await push_ota(IMAGE, "esp.local", CLIENT_ID, client)

    assert captured["method"] == "POST"
    assert captured["headers"][CLIENT_ID_HEADER.lower()] == CLIENT_ID
    assert captured["headers"]["content-type"] == OTA_CONTENT_TYPE
    assert captured["body"] == IMAGE
    # Helper returns the parsed JSON response so callers can show
    # which partition the device will boot into next.
    assert result == {"status": "flashing", "bytes": len(IMAGE)}


@respx.mock
async def test_push_ota_raises_auth_error_on_401():
    """401 must surface as PushAuthError so callers can distinguish
    'this device rejected us' from 'transport / image failure'."""
    respx.post("http://esp.local/ota").mock(
        return_value=httpx.Response(401, json={"error": "client id mismatch"})
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(PushAuthError):
            await push_ota(IMAGE, "esp.local", CLIENT_ID, client)


@respx.mock
async def test_push_ota_raises_push_error_on_400():
    """The firmware returns 400 when the image fails the bootloader's
    magic-byte / sha256 verification. The CLI must surface a non-zero
    exit so a bad-image push isn't silently accepted."""
    respx.post("http://esp.local/ota").mock(
        return_value=httpx.Response(400, text="image rejected by bootloader")
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(PushError, match="image rejected"):
            await push_ota(IMAGE, "esp.local", CLIENT_ID, client)


@respx.mock
async def test_push_ota_raises_push_error_on_network_failure():
    """A connection error during the multi-MB stream must surface as
    PushError, not silently leak as a generic httpx exception."""
    respx.post("http://esp.local/ota").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(PushError, match="Network error"):
            await push_ota(IMAGE, "esp.local", CLIENT_ID, client)


@respx.mock
async def test_push_ota_accepts_non_json_202_body():
    """The endpoint normally returns JSON, but a truncated socket
    after the status line could give us a bare 202. Tolerate that
    rather than crashing — the firmware is still rebooting."""
    respx.post("http://esp.local/ota").mock(
        return_value=httpx.Response(202, text="")
    )

    async with httpx.AsyncClient() as client:
        result = await push_ota(IMAGE, "esp.local", CLIENT_ID, client)
    assert result == {}
