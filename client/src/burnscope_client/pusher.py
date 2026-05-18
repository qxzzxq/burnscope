"""Push AgentSnapshot to the ESP32 over HTTP."""

from __future__ import annotations

import httpx

from .schema import AgentSnapshot


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
