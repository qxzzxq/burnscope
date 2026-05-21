# BurnScope

An always-on token & quota meter for your AI coding agents.

This repo has two components:

- `client/` — Python 3.11+ client. Two collectors with asymmetric lifecycles (see below). v1 is preserved at `client_old/` for reference.
- `firmware/` — ESP32 firmware (ESP-IDF, Cheap Yellow Display).

The wire format (`docs/wire-format.md`) is the daemon ↔ firmware contract — agent-agnostic, hand-mirrored in both languages.

## Multi-agent architecture (client v2)

v2 abandons v1's unified `Agent` ABC + asyncio daemon-loop. Each collector now reads from an agent-native, zero-cost source. Full spec: `docs/client-spec-v2.html`.

- `client/src/burnscope_client/schema.py` — `SessionSnapshot` and `AgentSnapshot` (frozen dataclasses). Unchanged from v1. Matches `docs/wire-format.md`.
- `client/src/burnscope_client/discovery.py` — mDNS browse for `_burnscope._tcp.local.`.
- `client/src/burnscope_client/identity.py` — Derives the per-agent `X-BurnScope-Client-Id` (SHA-256). `claude_org_uuid()` reads the system keyring with a `~/.claude/.credentials.json` fallback. Codex's email comes via the app-server `account/read` call in `codex_daemon.py`.
- `client/src/burnscope_client/host_cache.py` — Atomic file-backed state in `~/.burnscope/` (override with `BURNSCOPE_STATE_DIR`): cached host, per-agent `last-push.*` indicator.
- `client/src/burnscope_client/pusher.py` — `POST /summary` and `GET /health`, both sending the client-id header. `PushAuthError` (401) is distinct from `PushError` (transport / other) so callers know whether to invalidate the host cache.
- `client/src/burnscope_client/claude_statusline.py` — Claude Code statusline hook. Foreground mode renders the line and forks a detached `--push` child; `--push` mode resolves the host, derives the client_id, POSTs, and writes `last-push.claude`.
- `client/src/burnscope_client/codex_daemon.py` — Long-lived daemon owning a `codex app-server` subprocess. Reader/pusher/health coroutines; reconnects with exponential backoff on EOF.
- `client/src/burnscope_client/cli.py` — Installer/status tool, not a daemon entry. Subcommands: `install/uninstall {claude,codex}`, `status`, `pair-reset`.

To add an agent: write a per-fire script (Claude-style) or a long-lived daemon module (Codex-style). The shared modules are `schema`, `discovery`, `host_cache`, `identity`, `pusher`. Register a CLI install path that wires the supervisor (settings.json for Claude-style; launchd/systemd for daemon-style).

## Firmware

The MVP is currently being developed on a Cheap-Yellow-Display (CYD), the board is a `cyd2usb` variant (one USB-C and one micro-USB port) -- ST7789, 320×240 landscape.

## ESP-IDF

Path: `~/.espressif/v6.0.1/esp-idf`

## Pointers

- Code style: [./rules/code-style.md](./rules/code-style.md)
- Project description: [../docs/description.md](../docs/description.md)
- Wire format: [../docs/wire-format.md](../docs/wire-format.md)
- Client spec v2 (current): [../docs/client-spec-v2.html](../docs/client-spec-v2.html)
- Claude statusline source: [../docs/claude-statusline.html](../docs/claude-statusline.html)
- Codex app-server source: [../docs/codex-app-server.html](../docs/codex-app-server.html)
- Client spec v1 (historical): [../docs/client-spec.html](../docs/client-spec.html)
