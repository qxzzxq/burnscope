"""Tests for the `burnscope ota` CLI subcommand."""

from __future__ import annotations

import httpx
import pytest
import respx

from burnscope_client import cli, host_cache
from burnscope_client.host_cache import PairedDevice
from burnscope_client.pusher import CLIENT_ID_HEADER


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("BURNSCOPE_STATE_DIR", str(tmp_path))


@pytest.fixture
def image_path(tmp_path):
    """Write a 4 KiB pseudo-image starting with the ESP-IDF magic byte."""
    p = tmp_path / "burnscope.bin"
    p.write_bytes(b"\xe9" + b"\x00" * 4095)
    return p


def _pair_device(agent: str, device_id: str, host: str, *, client_id: str) -> None:
    host_cache.write_client_id(agent, client_id)
    host_cache.add_paired_device(agent, PairedDevice(device_id=device_id, host=host))


@respx.mock
def test_ota_pushes_to_paired_device_and_returns_zero(image_path, capsys):
    """Happy path: device is paired with the claude agent, image is
    valid, server returns 202. CLI exits 0 and prints the response."""
    _pair_device("claude", "burnscope-1234", "10.0.0.5:80",
                 client_id="claude@example.com")

    route = respx.post("http://10.0.0.5:80/ota").mock(
        return_value=httpx.Response(
            202, json={"status": "flashing", "bytes": 4096, "next_boot": "ota_1"}
        )
    )

    rc = cli.main(["ota", str(image_path), "--device", "burnscope-1234"])
    assert rc == 0
    assert route.called
    sent = route.calls.last.request
    assert sent.headers[CLIENT_ID_HEADER] == "claude@example.com"
    assert sent.content == image_path.read_bytes()
    out = capsys.readouterr().out
    assert "burnscope-1234" in out
    assert "ota_1" in out  # next_boot surfaced


def test_ota_errors_when_device_not_paired(image_path, capsys):
    """No pairing → exit 1 with a hint about how to fix it. No HTTP
    call is attempted; respx isn't even imported so an accidental
    network call would error rather than silently succeed."""
    rc = cli.main(["ota", str(image_path), "--device", "burnscope-9999"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "burnscope-9999" in out
    assert "pair" in out.lower()  # guidance toward `burnscope pair`


def test_ota_errors_when_image_missing(tmp_path, capsys):
    """Missing image path is a setup error — exit 1 before touching
    host_cache, so the user fixes the path rather than going hunting
    for a non-existent pairing."""
    rc = cli.main(["ota", str(tmp_path / "does-not-exist.bin"),
                   "--device", "burnscope-1234"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "does-not-exist.bin" in out


def test_ota_errors_when_image_is_empty(tmp_path, capsys):
    """An empty file would 400 from the firmware anyway, but failing
    locally saves the multi-MB upload + a roundtrip."""
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    _pair_device("claude", "burnscope-1234", "10.0.0.5:80",
                 client_id="claude@example.com")

    rc = cli.main(["ota", str(empty), "--device", "burnscope-1234"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "empty" in out.lower()


@respx.mock
def test_ota_surfaces_auth_failure_as_non_zero_exit(image_path, capsys):
    """A 401 from the firmware means the stored client_id no longer
    matches the device's bound slot (factory-reset on the device side,
    or pair-reset on the client side after a reflash). Exit non-zero
    so a CI/scripted reflash doesn't pretend success."""
    _pair_device("claude", "burnscope-1234", "10.0.0.5:80",
                 client_id="claude@example.com")
    respx.post("http://10.0.0.5:80/ota").mock(
        return_value=httpx.Response(401, json={"error": "client id mismatch"})
    )

    rc = cli.main(["ota", str(image_path), "--device", "burnscope-1234"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "401" in out or "auth" in out.lower()


@respx.mock
def test_ota_prefers_codex_pairing_when_device_only_paired_there(image_path):
    """Device might be in only one agent's paired list. The CLI must
    pick the agent that actually has the device — picking the wrong
    one would send a stale client_id and 401."""
    _pair_device("codex", "burnscope-1234", "10.0.0.5:80",
                 client_id="chatgpt@example.com")

    route = respx.post("http://10.0.0.5:80/ota").mock(
        return_value=httpx.Response(202, json={"status": "flashing", "bytes": 4096})
    )

    rc = cli.main(["ota", str(image_path), "--device", "burnscope-1234"])
    assert rc == 0
    sent = route.calls.last.request
    assert sent.headers[CLIENT_ID_HEADER] == "chatgpt@example.com"
