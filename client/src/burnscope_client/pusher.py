"""HTTP client for talking to the ESP32 — POST /summary and GET /health.

Both calls send the `X-BurnScope-Client-Id` header so the ESP32 can enforce
per-agent TOFU pairing. See `docs/client-spec-v2.html` § 3 (Identity &
pairing) and § 4 (Wire-format additions).

`push_to_all()` is the multi-device fan-out used by both collectors: it
runs per-device pushes in parallel and returns one `PushResult` per
device so the caller can drop on 401 and keep on transport failure
without writing its own gather logic.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import httpx

from .host_cache import PairedDevice
from .schema import AgentSnapshot

log = logging.getLogger(__name__)

CLIENT_ID_HEADER = "X-BurnScope-Client-Id"

PushKind = Literal["ok", "auth", "transport"]


class PushError(RuntimeError):
    """Raised on transport failure or any non-2xx that isn't auth-related."""


class PushAuthError(PushError):
    """Raised on 401 — device is reachable but our client_id doesn't match
    the bound slot. Callers must NOT silently drop the host on this; the
    multi-device caller drops only this `(agent, device_id)` from its
    paired list, leaving other agents' bindings to the same device intact.
    """


@dataclass(frozen=True)
class PushResult:
    """Per-device outcome from `push_to_all`.

    `kind` lets the caller distinguish:
      - "ok"        → record success.
      - "auth"      → silently drop this device from the agent's paired list.
      - "transport" → keep the device, record a per-device failure; retry
                      on the next fire.
    """

    device_id: str
    ok: bool
    kind: PushKind


async def push(
    snapshot: AgentSnapshot,
    host: str,
    client_id: str,
    client: httpx.AsyncClient,
) -> None:
    """POST `snapshot` to `http://{host}/summary` with the client-id header."""
    url = f"http://{host}/summary"
    headers = {CLIENT_ID_HEADER: client_id}
    log.debug(
        "POST %s agent=%s sessions=%d client_id=%s",
        url,
        snapshot.agent,
        len(snapshot.sessions),
        client_id,
    )
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
    log.info("POST %s → %d", url, response.status_code)


async def push_to_all(
    snapshot: AgentSnapshot,
    devices: list[PairedDevice],
    client_id: str,
    client: httpx.AsyncClient,
) -> dict[str, PushResult]:
    """Fan out `snapshot` to every device in `devices`, in parallel.

    Returns one `PushResult` per device, keyed by `device_id`. Exceptions
    from `push()` are translated into `kind="auth"` (PushAuthError) or
    `kind="transport"` (PushError / unexpected) so the caller never has
    to catch — it just inspects the result dict.

    Empty `devices` returns an empty dict.
    """
    if not devices:
        return {}

    async def _one(device: PairedDevice) -> PushResult:
        try:
            await push(snapshot, device.host, client_id, client)
        except PushAuthError as exc:
            log.info("push %s rejected with 401: %s", device.device_id, exc)
            return PushResult(device_id=device.device_id, ok=False, kind="auth")
        except PushError as exc:
            log.warning("push %s transport failure: %s", device.device_id, exc)
            return PushResult(device_id=device.device_id, ok=False, kind="transport")
        return PushResult(device_id=device.device_id, ok=True, kind="ok")

    results = await asyncio.gather(*(_one(d) for d in devices))
    return {r.device_id: r for r in results}


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
