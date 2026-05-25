"""HTTP client for the firmware's POST /ota endpoint.

Pushes a `.bin` image to a paired device. Mirrors the auth model and
exception taxonomy of `pusher.push()` — the firmware uses the same
`X-BurnScope-Client-Id` header and the same 401-vs-other split — so
callers can reason about results uniformly.

`PushAuthError` and `PushError` are re-exported from `pusher` rather
than introduced here, to keep the contract obvious: an OTA push fails
exactly the way a snapshot push does.
"""

from __future__ import annotations

import json
import logging

import httpx

from .pusher import CLIENT_ID_HEADER, PushAuthError, PushError

log = logging.getLogger(__name__)

OTA_CONTENT_TYPE = "application/octet-stream"

# The image is multi-MB and the device streams it through
# esp_ota_write at flash speeds; pick a timeout that comfortably
# covers a 5 MB upload over a slow LAN (~500 kbps worst case) plus
# the ~1 s reboot grace.
_OTA_TIMEOUT_SECONDS = 120.0


async def push_ota(
    image: bytes,
    host: str,
    client_id: str,
    client: httpx.AsyncClient,
) -> dict:
    """Stream `image` to `http://{host}/ota` with the client-id header.

    Returns the parsed JSON response body on success (typically
    `{"status": "flashing", "bytes": N, "next_boot": "ota_X"}`). If the
    server replied with an empty / non-JSON body but a 2xx status,
    returns `{}` so the caller doesn't have to special-case the
    truncated-socket-during-reboot edge.

    Raises:
      PushAuthError  — server returned 401 (client_id not paired, or
                       device hasn't been paired at all).
      PushError      — server returned any other 3xx/4xx/5xx, or a
                       transport error occurred during the stream.
    """
    url = f"http://{host}/ota"
    headers = {
        CLIENT_ID_HEADER: client_id,
        "Content-Type": OTA_CONTENT_TYPE,
    }
    log.info("POST %s — %d bytes", url, len(image))
    try:
        response = await client.post(
            url, content=image, headers=headers, timeout=_OTA_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        raise PushError(f"Network error pushing OTA to {url}: {exc}") from exc

    if response.status_code == 401:
        raise PushAuthError(
            f"{url} returned 401 — client_id not paired with this device"
        )
    if response.status_code >= 300:
        raise PushError(
            f"{url} returned {response.status_code}: {response.text[:200]}"
        )

    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        # Firmware schedules a reboot ~1 s after the 202; under tight
        # timing the socket can close before the JSON envelope flushes.
        # The OTA itself was accepted — surface success.
        return {}
    if not isinstance(body, dict):
        return {}
    return body
