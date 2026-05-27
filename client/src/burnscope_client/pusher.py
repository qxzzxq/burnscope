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

from . import host_cache
from .discovery import discover_all
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

    Returns one `PushResult` per device, keyed by `device_id`. The two
    exception types `push()` raises are translated:
      - `PushAuthError` → `kind="auth"`
      - `PushError`     → `kind="transport"`
    Any other exception (programmer error, `asyncio.CancelledError`)
    propagates and aborts the whole fan-out, by design.

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


async def refresh_and_retry_transport_failures(
    snapshot: AgentSnapshot,
    devices: list[PairedDevice],
    results: dict[str, PushResult],
    client_id: str,
    client: httpx.AsyncClient,
    agent: str,
    *,
    discovery_timeout: float = 4.0,
) -> dict[str, PushResult]:
    """Re-resolve transport-failed devices via mDNS and retry once.

    A device's cached `host` can go stale after a DHCP renewal. The
    `device_id` (mDNS hostname, MAC-derived) is stable across IP
    changes, so a "transport failure" that's really just a moved IP can
    be healed by browsing for the device by id and retrying the push at
    its new host.

    Throttling: each `(agent, device_id)` is gated by
    `host_cache.claim_reconcile_slots`, which keeps a powered-off device
    from forcing a fresh full-LAN browse on every statusline fire. If
    every transport-failed device is inside its cooldown window, the
    helper returns results unchanged with no browse.

    Update-only writes: a refreshed host is committed via
    `host_cache.update_paired_device_host`. If that returns False — the
    device was removed concurrently by a `/summary` 401 or `pair-reset`
    while discovery was in flight — the retry is dropped so stale mDNS
    data cannot resurrect a forgotten pairing.

    For each entry in `results` with `kind="transport"` that won a
    cooldown slot:
      - Browse `_burnscope._tcp.local`.
      - If the device shows up at a *different* host: try the update-only
        cache write, then re-push to the new host on success.
      - If the device shows up at the same host, doesn't show up at all,
        or its pairing has just been removed: leave the result unchanged.

    Returns a fresh dict; entries for retried devices are replaced with
    the retry outcome.
    """
    transport_failed = [
        did for did, r in results.items() if r.kind == "transport"
    ]
    if not transport_failed:
        return dict(results)

    eligible = host_cache.claim_reconcile_slots(agent, transport_failed)
    if not eligible:
        log.debug(
            "all %d transport-failed device(s) inside mDNS reconcile cooldown; "
            "skipping browse",
            len(transport_failed),
        )
        return dict(results)

    discovered = await discover_all(timeout=discovery_timeout)
    by_id = {d.device_id: d for d in discovered}
    old_hosts = {d.device_id: d.host for d in devices}

    updated = dict(results)
    for device_id in eligible:
        found = by_id.get(device_id)
        if found is None:
            continue
        if found.host == old_hosts.get(device_id):
            continue
        if not host_cache.update_paired_device_host(agent, device_id, found.host):
            log.info(
                "skipped retry for %s — pairing was removed during reconcile",
                device_id,
            )
            continue
        log.info(
            "host changed for %s (%s -> %s); retrying push",
            device_id, old_hosts.get(device_id), found.host,
        )
        refreshed = PairedDevice(device_id=device_id, host=found.host)
        retry = await push_to_all(snapshot, [refreshed], client_id, client)
        updated[device_id] = retry[device_id]
    return updated


def _duplicate_host_groups(
    devices: list[PairedDevice],
) -> dict[str, list[str]]:
    """Return `{host: [device_id, ...]}` for hosts shared by 2+ devices.

    A unique cached `host` proves who replied to an HTTP request; a
    shared `host` means we can't tell. The resilience plan calls this
    an identity conflict and treats it like a reachability failure
    until reconciliation can split the records.
    """
    by_host: dict[str, list[str]] = {}
    for device in devices:
        by_host.setdefault(device.host, []).append(device.device_id)
    return {host: ids for host, ids in by_host.items() if len(ids) > 1}


async def reconcile_duplicate_hosts(
    agent: str,
    devices: list[PairedDevice],
    *,
    discovery_timeout: float = 4.0,
) -> set[str]:
    """Resolve cached duplicate-host groups via one throttled mDNS pass.

    A `host` in `paired-devices.<agent>.json` should normally identify a
    single device. If two records share it, an HTTP success against
    that host cannot identify which physical display replied — so the
    caller must not declare per-device success. This helper:

      * does nothing if no host is shared (short-circuit; no browse);
      * gates the browse through `host_cache.claim_reconcile_slots`,
        sharing the per-`(agent, device_id)` cooldown with the
        transport-failure path so the two cannot double-trigger a
        browse within the cooldown window;
      * for each visible device with a new host, commits the refresh
        via `host_cache.update_paired_device_host` (update-only — never
        resurrects a pairing that was removed during the browse);
      * reloads the paired list and returns the set of device_ids that
        STILL share a cached host with another paired entry.

    The returned "unverified" set is the caller's signal that those
    devices may not be marked healthy from any shared HTTP response.
    Pairings are NEVER removed by this helper.
    """
    groups = _duplicate_host_groups(devices)
    if not groups:
        return set()
    affected_ids = [did for ids in groups.values() for did in ids]
    eligible = host_cache.claim_reconcile_slots(agent, affected_ids)
    if not eligible:
        log.debug(
            "duplicate-host reconcile for %d device(s) blocked by cooldown",
            len(affected_ids),
        )
        return set(affected_ids)

    try:
        discovered = await discover_all(timeout=discovery_timeout)
    except Exception as exc:
        # mDNS failure on this path must not bubble up; keep the
        # affected devices unverified so the caller doesn't claim
        # spurious success, and let the next eligible cycle try again.
        log.warning("mDNS browse during duplicate-host reconcile failed: %s", exc)
        return set(affected_ids)

    by_id = {d.device_id: d for d in discovered}
    old_hosts = {d.device_id: d.host for d in devices}
    for device_id in eligible:
        found = by_id.get(device_id)
        if found is None or found.host == old_hosts.get(device_id):
            continue
        host_cache.update_paired_device_host(agent, device_id, found.host)

    # Recompute after refresh — devices whose hosts still alias another
    # paired record remain unverified for this cycle.
    refreshed = host_cache.load_paired_devices(agent)
    still_conflicting = _duplicate_host_groups(refreshed)
    return {did for ids in still_conflicting.values() for did in ids}


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
