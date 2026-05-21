"""HTTP client for talking to the ESP32 — POST /summary and GET /health."""

from __future__ import annotations

import logging

import httpx

from .schema import AgentSnapshot

log = logging.getLogger(__name__)


class PushError(RuntimeError):
    """Raised when POST /summary fails (network or non-2xx)."""


async def push(snapshot: AgentSnapshot, host: str, client: httpx.AsyncClient) -> None:
    """POST `snapshot` to `http://{host}/summary`. Expects 2xx."""
    url = f"http://{host}/summary"
    try:
        response = await client.post(url, json=snapshot.to_dict(), timeout=5.0)
    except httpx.HTTPError as exc:
        raise PushError(f"Network error pushing to {url}: {exc}") from exc

    if response.status_code >= 300:
        raise PushError(
            f"{url} returned {response.status_code}: {response.text[:200]}"
        )


async def fetch_health(host: str, client: httpx.AsyncClient) -> dict | None:
    """GET /health and return the parsed JSON body.

    Returns None on transport error, non-2xx, or invalid JSON. The daemon
    treats None as "device unreachable" and triggers mDNS rediscovery; the
    parsed body lets it compare firmware-side stored snapshots against the
    latest probe to decide whether `/summary` needs a re-POST.
    """
    url = f"http://{host}/health"
    try:
        response = await client.get(url, timeout=5.0)
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
