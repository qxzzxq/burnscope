import json

import httpx
import pytest
import respx

from burnscope_client import host_cache, pusher
from burnscope_client.discovery import DiscoveredDevice
from burnscope_client.host_cache import PairedDevice
from burnscope_client.pusher import (
    CLIENT_ID_HEADER,
    PushAuthError,
    PushError,
    PushResult,
    fetch_health,
    push,
    push_to_all,
    reconcile_duplicate_hosts,
    refresh_and_retry_transport_failures,
)
from burnscope_client.schema import AgentSnapshot, SessionSnapshot


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))
    return tmp_path


CLIENT_ID = "a" * 64


def _snapshot() -> AgentSnapshot:
    return AgentSnapshot(
        agent="claude",
        captured_at=1779050146,
        sessions=[
            SessionSnapshot("current", 0.03, 1779066600),
            SessionSnapshot("weekly", 0.09, 1779156000),
        ],
    )


@respx.mock
async def test_push_posts_snapshot_with_client_id_header():
    route = respx.post("http://esp.local/summary").mock(
        return_value=httpx.Response(204)
    )

    async with httpx.AsyncClient() as client:
        await push(_snapshot(), "esp.local", CLIENT_ID, client)

    assert route.called
    request = route.calls[0].request
    assert request.headers[CLIENT_ID_HEADER] == CLIENT_ID
    assert json.loads(request.read()) == _snapshot().to_dict()


@respx.mock
async def test_push_401_raises_push_auth_error():
    respx.post("http://esp.local/summary").mock(return_value=httpx.Response(401))
    async with httpx.AsyncClient() as client:
        with pytest.raises(PushAuthError):
            await push(_snapshot(), "esp.local", CLIENT_ID, client)


@respx.mock
async def test_push_500_raises_push_error_not_auth():
    respx.post("http://esp.local/summary").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as client:
        with pytest.raises(PushError) as exc_info:
            await push(_snapshot(), "esp.local", CLIENT_ID, client)
    assert not isinstance(exc_info.value, PushAuthError)


@respx.mock
async def test_push_transport_error_raises_push_error():
    respx.post("http://esp.local/summary").mock(
        side_effect=httpx.ConnectError("boom")
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(PushError):
            await push(_snapshot(), "esp.local", CLIENT_ID, client)


@respx.mock
async def test_fetch_health_sends_client_id_header_and_returns_body():
    route = respx.get("http://esp.local/health").mock(
        return_value=httpx.Response(200, json={"agents": {}})
    )
    async with httpx.AsyncClient() as client:
        body = await fetch_health("esp.local", CLIENT_ID, client)

    assert body == {"agents": {}}
    assert route.calls[0].request.headers[CLIENT_ID_HEADER] == CLIENT_ID


@respx.mock
async def test_fetch_health_returns_none_on_transport_failure():
    respx.get("http://esp.local/health").mock(
        side_effect=httpx.ConnectError("nope")
    )
    async with httpx.AsyncClient() as client:
        assert await fetch_health("esp.local", CLIENT_ID, client) is None


@respx.mock
async def test_fetch_health_returns_auth_sentinel_on_401():
    # A reachable device whose slot our client_id doesn't match returns 401.
    # This must be distinguishable from a transport failure so the daemon
    # can treat it as a re-bind trigger rather than a reachability miss
    # (issue #66).
    respx.get("http://esp.local/health").mock(
        return_value=httpx.Response(401, json={"error": "client id mismatch"})
    )
    async with httpx.AsyncClient() as client:
        result = await fetch_health("esp.local", CLIENT_ID, client)
    assert result is pusher.HEALTH_AUTH_REJECTED


@respx.mock
async def test_fetch_health_returns_none_on_non_401_error():
    # Only 401 is special; every other non-2xx still collapses to None so
    # the existing "treat as unreachable" path is unchanged.
    respx.get("http://esp.local/health").mock(
        return_value=httpx.Response(503)
    )
    async with httpx.AsyncClient() as client:
        assert await fetch_health("esp.local", CLIENT_ID, client) is None


# ----------------------------------------------------------- push_to_all

async def test_push_to_all_returns_empty_for_no_devices():
    async with httpx.AsyncClient() as client:
        results = await push_to_all(_snapshot(), [], CLIENT_ID, client)
    assert results == {}


@respx.mock
async def test_push_to_all_fans_out_in_parallel_with_all_success():
    respx.post("http://10.0.0.5:80/summary").mock(return_value=httpx.Response(204))
    respx.post("http://10.0.0.6:80/summary").mock(return_value=httpx.Response(204))
    devices = [
        PairedDevice("dev-a", "10.0.0.5:80"),
        PairedDevice("dev-b", "10.0.0.6:80"),
    ]
    async with httpx.AsyncClient() as client:
        results = await push_to_all(_snapshot(), devices, CLIENT_ID, client)

    assert set(results) == {"dev-a", "dev-b"}
    assert all(r.kind == "ok" and r.ok for r in results.values())


@respx.mock
async def test_push_to_all_isolates_per_device_outcomes():
    """One 401 + one 500 + one 204 should yield exactly that mix."""
    respx.post("http://10.0.0.5:80/summary").mock(return_value=httpx.Response(204))
    respx.post("http://10.0.0.6:80/summary").mock(return_value=httpx.Response(401))
    respx.post("http://10.0.0.7:80/summary").mock(return_value=httpx.Response(500))
    devices = [
        PairedDevice("dev-ok",    "10.0.0.5:80"),
        PairedDevice("dev-auth",  "10.0.0.6:80"),
        PairedDevice("dev-trans", "10.0.0.7:80"),
    ]
    async with httpx.AsyncClient() as client:
        results = await push_to_all(_snapshot(), devices, CLIENT_ID, client)

    assert results["dev-ok"]    == PushResult("dev-ok",    True,  "ok")
    assert results["dev-auth"]  == PushResult("dev-auth",  False, "auth")
    assert results["dev-trans"] == PushResult("dev-trans", False, "transport")


@respx.mock
async def test_push_to_all_handles_transport_exception():
    respx.post("http://10.0.0.6:80/summary").mock(
        side_effect=httpx.ConnectError("dropped")
    )
    devices = [PairedDevice("dev-down", "10.0.0.6:80")]
    async with httpx.AsyncClient() as client:
        results = await push_to_all(_snapshot(), devices, CLIENT_ID, client)
    assert results["dev-down"].kind == "transport"


@respx.mock
async def test_push_to_all_sends_client_id_header_per_device():
    route_a = respx.post("http://10.0.0.5:80/summary").mock(
        return_value=httpx.Response(204)
    )
    route_b = respx.post("http://10.0.0.6:80/summary").mock(
        return_value=httpx.Response(204)
    )
    async with httpx.AsyncClient() as client:
        await push_to_all(
            _snapshot(),
            [PairedDevice("a", "10.0.0.5:80"), PairedDevice("b", "10.0.0.6:80")],
            CLIENT_ID,
            client,
        )
    assert route_a.calls[0].request.headers[CLIENT_ID_HEADER] == CLIENT_ID
    assert route_b.calls[0].request.headers[CLIENT_ID_HEADER] == CLIENT_ID


# -------------------------------------------- refresh_and_retry_transport_failures


def _stub_discover(monkeypatch, devices):
    async def fake(timeout=4.0, agent=None, zc=None):
        return list(devices)
    monkeypatch.setattr(pusher, "discover_all", fake)


async def test_refresh_and_retry_noop_when_no_transport_failures(monkeypatch):
    """No transport failures → no mDNS browse, results returned as-is."""
    def boom(*a, **kw):
        raise AssertionError("discover_all must not run without transport failures")
    monkeypatch.setattr(pusher, "discover_all", boom)

    results = {
        "dev-a": PushResult("dev-a", True,  "ok"),
        "dev-b": PushResult("dev-b", False, "auth"),
    }
    async with httpx.AsyncClient() as client:
        out = await refresh_and_retry_transport_failures(
            _snapshot(),
            [PairedDevice("dev-a", "10.0.0.5:80"), PairedDevice("dev-b", "10.0.0.6:80")],
            results, CLIENT_ID, client, "claude",
        )
    assert out == results


@respx.mock
async def test_refresh_and_retry_updates_cache_and_retries_on_new_host(monkeypatch):
    """The headline bug: device's IP changed → discover new host, update cache, retry."""
    host_cache.add_paired_device("claude", PairedDevice("dev-moved", "10.0.0.5:80"))
    _stub_discover(monkeypatch, [
        DiscoveredDevice("dev-moved", "10.0.0.9:80", True, False),
    ])
    new_route = respx.post("http://10.0.0.9:80/summary").mock(
        return_value=httpx.Response(204)
    )
    results = {"dev-moved": PushResult("dev-moved", False, "transport")}
    devices = [PairedDevice("dev-moved", "10.0.0.5:80")]

    async with httpx.AsyncClient() as client:
        out = await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )

    assert out["dev-moved"] == PushResult("dev-moved", True, "ok")
    assert new_route.called
    cached = host_cache.load_paired_devices("claude")
    assert cached == [PairedDevice("dev-moved", "10.0.0.9:80")]


async def test_refresh_and_retry_skips_when_host_unchanged(monkeypatch):
    """mDNS confirms same host → no retry (the failure was truly transient)."""
    _stub_discover(monkeypatch, [
        DiscoveredDevice("dev-same", "10.0.0.5:80", True, False),
    ])
    results = {"dev-same": PushResult("dev-same", False, "transport")}
    devices = [PairedDevice("dev-same", "10.0.0.5:80")]

    async with httpx.AsyncClient() as client:
        out = await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )
    assert out == results


async def test_refresh_and_retry_keeps_failure_when_device_not_discovered(monkeypatch):
    """mDNS didn't see the device → keep original transport failure, don't retry."""
    _stub_discover(monkeypatch, [])
    results = {"dev-gone": PushResult("dev-gone", False, "transport")}
    devices = [PairedDevice("dev-gone", "10.0.0.5:80")]

    async with httpx.AsyncClient() as client:
        out = await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )
    assert out == results


@respx.mock
async def test_refresh_and_retry_leaves_other_devices_untouched(monkeypatch):
    """Only the transport-failed device is mDNS-refreshed; others pass through."""
    host_cache.add_paired_device("claude", PairedDevice("dev-moved", "10.0.0.5:80"))
    _stub_discover(monkeypatch, [
        DiscoveredDevice("dev-moved", "10.0.0.9:80", True, False),
        DiscoveredDevice("dev-ok",    "10.0.0.6:80", True, False),
    ])
    respx.post("http://10.0.0.9:80/summary").mock(return_value=httpx.Response(204))
    results = {
        "dev-ok":    PushResult("dev-ok",    True,  "ok"),
        "dev-moved": PushResult("dev-moved", False, "transport"),
    }
    devices = [
        PairedDevice("dev-ok",    "10.0.0.6:80"),
        PairedDevice("dev-moved", "10.0.0.5:80"),
    ]

    async with httpx.AsyncClient() as client:
        out = await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )
    assert out["dev-ok"] == PushResult("dev-ok", True, "ok")
    assert out["dev-moved"] == PushResult("dev-moved", True, "ok")


# ============================== mDNS resilience §2: throttle + update-only

async def test_refresh_and_retry_skips_browse_when_cooldown_active(monkeypatch):
    """Two transport failures inside the same cooldown window must not
    trigger two mDNS browses for the same device — that pummels mDNS for
    a powered-off display. The second call records the failure but
    short-circuits the browse via claim_reconcile_slots."""
    host_cache.add_paired_device("claude", PairedDevice("dev-down", "10.0.0.5:80"))
    # First call gets the slot and runs a browse.
    browses = 0

    async def counting_discover(timeout=4.0, agent=None, zc=None):
        nonlocal browses
        browses += 1
        return []

    monkeypatch.setattr(pusher, "discover_all", counting_discover)

    results = {"dev-down": PushResult("dev-down", False, "transport")}
    devices = [PairedDevice("dev-down", "10.0.0.5:80")]

    async with httpx.AsyncClient() as client:
        # First call: cooldown is cold → browse runs.
        await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )
        # Second call: cooldown still active → no second browse.
        await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )

    assert browses == 1


@respx.mock
async def test_refresh_and_retry_drops_retry_when_device_unpaired_concurrently(
    monkeypatch,
):
    """If a concurrent path (e.g. /summary 401, pair-reset) removed the
    device while discovery was in flight, update_paired_device_host
    returns False and the retry must be skipped — never resurrect a
    forgotten pairing.
    """
    host_cache.add_paired_device("claude", PairedDevice("dev-moved", "10.0.0.5:80"))

    async def fake_discover(timeout=4.0, agent=None, zc=None):
        # Simulate the race: remove the pairing while mDNS browses.
        host_cache.remove_paired_device("claude", "dev-moved")
        return [DiscoveredDevice("dev-moved", "10.0.0.9:80", True, False)]

    monkeypatch.setattr(pusher, "discover_all", fake_discover)
    # If the retry runs anyway, this mock route would catch it — and
    # then the assertion below fails. We deliberately don't register
    # the route so any retry attempt would also raise an unmatched-route
    # error from respx.

    results = {"dev-moved": PushResult("dev-moved", False, "transport")}
    devices = [PairedDevice("dev-moved", "10.0.0.5:80")]

    async with httpx.AsyncClient() as client:
        out = await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )

    # Original transport failure preserved; no resurrected entry on disk.
    assert out["dev-moved"].kind == "transport"
    assert host_cache.load_paired_devices("claude") == []


# =============================== mDNS resilience §3: reconcile_duplicate_hosts

async def test_reconcile_duplicate_hosts_no_browse_when_no_duplicates(monkeypatch):
    """Unique cached hosts → no mDNS browse, no unverified set."""
    async def boom(*a, **kw):
        raise AssertionError("discover_all must not run without duplicate hosts")
    monkeypatch.setattr(pusher, "discover_all", boom)

    devices = [
        PairedDevice("dev-a", "10.0.0.5:80"),
        PairedDevice("dev-b", "10.0.0.6:80"),
    ]
    unverified = await reconcile_duplicate_hosts("claude", devices)
    assert unverified == set()


async def test_reconcile_duplicate_hosts_splits_via_mdns(monkeypatch):
    """Two paired records share a stale host; mDNS reveals their real
    distinct hosts; update_paired_device_host splits them; nobody ends
    up unverified."""
    host_cache.add_paired_device("claude", PairedDevice("dev-a", "10.0.0.5:80"))
    host_cache.add_paired_device("claude", PairedDevice("dev-b", "10.0.0.5:80"))
    devices = host_cache.load_paired_devices("claude")

    async def fake_discover(timeout=4.0, agent=None, zc=None):
        return [
            DiscoveredDevice("dev-a", "10.0.0.5:80", True, False),
            DiscoveredDevice("dev-b", "10.0.0.7:80", True, False),
        ]
    monkeypatch.setattr(pusher, "discover_all", fake_discover)

    unverified = await reconcile_duplicate_hosts("claude", devices)
    assert unverified == set()
    final = {d.device_id: d.host for d in host_cache.load_paired_devices("claude")}
    assert final == {"dev-a": "10.0.0.5:80", "dev-b": "10.0.0.7:80"}


async def test_reconcile_duplicate_hosts_marks_unresolved_unverified(monkeypatch):
    """mDNS can't see either of the conflicting devices → both stay
    paired and the set of unverified ids is returned so the caller
    won't mark them healthy from a shared HTTP response."""
    host_cache.add_paired_device("claude", PairedDevice("dev-a", "10.0.0.5:80"))
    host_cache.add_paired_device("claude", PairedDevice("dev-b", "10.0.0.5:80"))
    devices = host_cache.load_paired_devices("claude")

    async def fake_discover(timeout=4.0, agent=None, zc=None):
        return []
    monkeypatch.setattr(pusher, "discover_all", fake_discover)

    unverified = await reconcile_duplicate_hosts("claude", devices)
    assert unverified == {"dev-a", "dev-b"}
    # Pairings preserved (plan §3: never remove from this path).
    assert len(host_cache.load_paired_devices("claude")) == 2


async def test_reconcile_duplicate_hosts_throttles_browses(monkeypatch):
    """Repeated calls inside the cooldown must not browse twice for the
    same conflict — the throttle is shared with the transport-failure
    path so they can't double-trigger a browse for the same device_id."""
    host_cache.add_paired_device("claude", PairedDevice("dev-a", "10.0.0.5:80"))
    host_cache.add_paired_device("claude", PairedDevice("dev-b", "10.0.0.5:80"))
    devices = host_cache.load_paired_devices("claude")

    browses = 0

    async def counting_discover(timeout=4.0, agent=None, zc=None):
        nonlocal browses
        browses += 1
        return []
    monkeypatch.setattr(pusher, "discover_all", counting_discover)

    await reconcile_duplicate_hosts("claude", devices)
    await reconcile_duplicate_hosts("claude", devices)

    assert browses == 1


async def test_reconcile_duplicate_hosts_marks_cooldown_blocked_as_unverified(
    monkeypatch,
):
    """If the throttle blocks the browse, the conflict cannot be
    resolved this cycle — every device in the duplicate-host group
    must be reported as unverified so the caller doesn't claim
    per-device success from a shared HTTP response."""
    host_cache.add_paired_device("claude", PairedDevice("dev-a", "10.0.0.5:80"))
    host_cache.add_paired_device("claude", PairedDevice("dev-b", "10.0.0.5:80"))
    # Pre-warm the cooldown so the next call short-circuits.
    host_cache.claim_reconcile_slots(
        "claude", ["dev-a", "dev-b"], now=1_000_000.0, cooldown_s=3600.0,
    )

    async def boom(*a, **kw):
        raise AssertionError("cooldown should block the browse")
    monkeypatch.setattr(pusher, "discover_all", boom)

    # Default 60 s cooldown is overridden by env in this test? No —
    # we just pre-claimed with 3600 s, so default 60 s still blocks.
    devices = host_cache.load_paired_devices("claude")
    unverified = await reconcile_duplicate_hosts("claude", devices)
    assert unverified == {"dev-a", "dev-b"}


async def test_reconcile_duplicate_hosts_keeps_unmoved_collider_unverified(
    monkeypatch,
):
    """If mDNS finds one of the duplicates at a new host but the other
    only at the same shared host, the collider that didn't move stays
    unverified — its cached host still aliases another paired entry."""
    host_cache.add_paired_device("claude", PairedDevice("dev-a", "10.0.0.5:80"))
    host_cache.add_paired_device("claude", PairedDevice("dev-b", "10.0.0.5:80"))
    devices = host_cache.load_paired_devices("claude")

    async def fake_discover(timeout=4.0, agent=None, zc=None):
        return [
            DiscoveredDevice("dev-a", "10.0.0.5:80", True, False),
            # dev-b not visible — can't refresh its host.
        ]
    monkeypatch.setattr(pusher, "discover_all", fake_discover)

    unverified = await reconcile_duplicate_hosts("claude", devices)
    # Both ids end up in the unverified set because their cached hosts
    # still alias after the partial reconciliation.
    assert unverified == {"dev-a", "dev-b"}


@respx.mock
async def test_refresh_and_retry_uses_update_only_not_insert(monkeypatch):
    """A successful refresh must update the existing entry without
    growing the paired list — proves the helper went through
    update_paired_device_host rather than add_paired_device."""
    host_cache.add_paired_device("claude", PairedDevice("dev-moved", "10.0.0.5:80"))
    host_cache.add_paired_device("claude", PairedDevice("dev-peer", "10.0.0.6:80"))

    async def fake_discover(timeout=4.0, agent=None, zc=None):
        return [DiscoveredDevice("dev-moved", "10.0.0.9:80", True, False)]

    monkeypatch.setattr(pusher, "discover_all", fake_discover)
    respx.post("http://10.0.0.9:80/summary").mock(return_value=httpx.Response(204))

    results = {"dev-moved": PushResult("dev-moved", False, "transport")}
    devices = [PairedDevice("dev-moved", "10.0.0.5:80")]

    async with httpx.AsyncClient() as client:
        await refresh_and_retry_transport_failures(
            _snapshot(), devices, results, CLIENT_ID, client, "claude",
        )

    cached = host_cache.load_paired_devices("claude")
    by_id = {d.device_id: d.host for d in cached}
    assert by_id == {"dev-moved": "10.0.0.9:80", "dev-peer": "10.0.0.6:80"}
