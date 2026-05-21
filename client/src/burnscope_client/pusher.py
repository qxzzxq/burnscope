"""HTTP client for talking to the ESP32 — POST /summary and GET /health.

Both calls send the `X-BurnScope-Client-Id` header so the ESP32 can enforce
per-agent TOFU pairing. The header value is the SHA-256 hex derived in
`identity.py`. See `docs/client-spec-v2.html` § 3 (Identity & pairing) and
§ 4 (Wire-format additions).
"""

from __future__ import annotations

import logging

import httpx

from .schema import AgentSnapshot

log = logging.getLogger(__name__)

CLIENT_ID_HEADER = "X-BurnScope-Client-Id"


class PushError(RuntimeError):
    """Raised on transport failure or any non-2xx that isn't auth-related."""


class PushAuthError(PushError):
    """Raised on 401 — host is reachable but our client_id doesn't match the
    paired hash. Callers must NOT invalidate the host cache for this error.
    """


async def push(
    snapshot: AgentSnapshot,
    host: str,
    client_id: str,
    client: httpx.AsyncClient,
) -> None:
    """POST `snapshot` to `http://{host}/summary` with the client-id header."""
    url = f"http://{host}/summary"
    headers = {CLIENT_ID_HEADER: client_id}
    try:
        response = await client.post(
            url, json=snapshot.to_dict(), headers=headers, timeout=5.0
        )
    except httpx.HTTPError as exc:
        raise PushError(f"Network error pushing to {url}: {exc}") from exc

    if response.status_code == 401:
        raise PushAuthError(
            f"{url} returned 401 — client_id not paired with this device"
        )
    if response.status_code >= 300:
        raise PushError(
            f"{url} returned {response.status_code}: {response.text[:200]}"
        )


async def fetch_health(
    host: str,
    client_id: str,
    client: httpx.AsyncClient,
) -> dict | None:
    """GET /health and return the parsed JSON body, or None on failure.

    None covers transport errors, non-2xx, and invalid JSON. The caller treats
    None as "device unreachable" and invalidates the host cache. 401 is
    distinct: it returns None too, but the daemon learns about auth issues
    through the push path, not health.
    """
    url = f"http://{host}/health"
    headers = {CLIENT_ID_HEADER: client_id}
    try:
        response = await client.get(url, headers=headers, timeout=5.0)
    except httpx.HTTPError as exc:
        log.warning("health %s failed: %s", url, exc)
        return None
    if response.status_code >= 300:
        log.warning("health %s returned %d", url, response.status_code)
        return None
    try:
        body = response.json()
    except ValueError as exc:
        log.warning("health %s returned invalid JSON: %s", url, exc)
        return None
    if not isinstance(body, dict):
        log.warning("health %s returned non-object body: %r", url, body)
        return None
    return body
