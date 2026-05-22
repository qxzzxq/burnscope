# BurnScope

> An always-on token & quota meter for your AI coding agents.

BurnScope tracks token usage for your AI coding agents and surfaces it on a dedicated ESP32 desk display. Per-agent collectors on your laptop read the session data the agent already produces (no upstream API calls, no header scraping), discover the ESP32 over mDNS, and push the latest numbers straight to it over HTTP. No intermediate server.

## Why a hardware display

Claude Code (and most subscription-based agents) work in fixed usage windows — for Claude Code, a 5-hour rolling window. When you hit the cap and walk away, there's no good way to know when the quota refreshes without unlocking a laptop or opening a tab. An always-on display answers "how much have I used / when does it reset" at a glance.

---

## Scope

**MVP (this document):** one machine, two agents (Claude Code and Codex CLI), one board (CYD), one API endpoint. Goal is end-to-end: a real token count from a real session appears on the display and counts down to window reset. Each agent has its own lifecycle — Claude is a per-fire statusline hook, Codex is a long-lived daemon — and either can be installed independently via `burnscope-client install <agent>`.

**Deferred to Phase 2:** multi-machine aggregation, additional agents (Gemini, Copilot, …), additional boards & layout families, web dashboard, auth, OTA, cost/$ estimation, persistent storage of snapshots, and an intermediate aggregation server (see note below).

> **Note on the cut server.** An earlier draft of this document put a Go aggregation server between the daemon and the ESP32. It was cut for the single-user MVP: for one laptop and one display it added two installs and a second always-on process without buying anything. It returns in Phase 2 only if it earns its keep — multi-machine aggregation, non-session agent schemas (credits, overage) that need shared state, or auth. The firmware contract (`POST /summary`) is designed to stay stable in that case: a future server simply takes the daemon's place as the thing speaking it.

---

## Architecture

```
┌───────────────────────────────┐       POST /summary           ┌────────────────────┐
│  Per-agent collectors          │ ────────────────────────────▶ │  ESP32 (CYD)       │
│   - Claude statusline hook     │   AgentSnapshot JSON          │   - mDNS advert    │
│   - Codex app-server daemon    │   + X-BurnScope-Client-Id     │   - HTTP server    │
│  Shared: schema, discovery,    │                               │   - NVS pairing    │
│  identity, pusher, host_cache  │                               │   - TFT renderer   │
└───────────────────────────────┘                               └────────────────────┘
```

- **Per-agent collectors** — each agent reads from its own zero-cost native source and builds an `AgentSnapshot` matching [wire-format.md](./wire-format.md). Claude is a Claude Code statusline hook that fires after each assistant message and forks a detached `--push` child; Codex is a long-lived daemon owning a `codex app-server` JSON-RPC subprocess that emits snapshots on `account/rateLimits/updated` notifications. There is no unified daemon and no header-scrape probing. Each collector reads a plaintext identifier (`oauthAccount.emailAddress` for Claude, `account.email` for Codex) and ships it in `X-BurnScope-Client-Id`.
- **ESP32 firmware** — advertises itself over mDNS as `_burnscope._tcp.local` on boot, runs a small HTTP server accepting `POST /summary` and `GET /health`, enforces per-agent TOFU pairing on the client-id header, rejects out-of-order pushes with a monotonic `captured_at` guard, and renders the last snapshot per agent. Holds no rolling-window state of its own — the collectors do the math.
- **Discovery** — collectors use `zeroconf` to find the advertised service and cache the resolved `host:port` under `~/.burnscope/host`. On a transport failure the cached host is invalidated so the next attempt rediscovers via mDNS.

---

## HTTP API

Two endpoints, on the ESP32:

| Endpoint              | Direction          | Body / Headers                                                                   | Response                                                            |
| --------------------- | ------------------ | -------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| `POST /summary`       | collector → ESP32  | a single `AgentSnapshot` + `X-BurnScope-Client-Id`                               | `204 No Content`, `401` (missing/mismatched id), `409` (stale `captured_at`) |
| `GET /health`         | collector → ESP32  | `X-BurnScope-Client-Id` (must match any bound slot once one exists)              | firmware version, uptime, free heap, per-agent `client_id` + `seconds_since_last_push` + `sessions` |

A network-triggered factory reset isn't exposed in MVP — the route was pulled because it would let anything on the LAN wipe the device. Reset is via the BOOT-button long-press (≥ 5 s); a network endpoint will return once an auth scheme lands. Schemas are hand-written in each language (Python, C). See [wire-format.md](./wire-format.md) for the full contract, including TOFU pairing semantics and the monotonic `captured_at` guard. Schema-as-codegen is deferred until there's a third consumer.

---

## Tech Stack

| Component       | Choice                                                              |
| --------------- | ------------------------------------------------------------------- |
| Firmware        | ESP-IDF v6.x, C, `esp_lcd_ili9341` + LVGL, `mdns`, `esp_http_server` |
| Board           | Cheap Yellow Display (ESP32-2432S028R), 320×240                     |
| Daemon          | Python 3.11+ (`httpx`, `zeroconf`)                                  |

WiFi credentials are captured on first boot via a captive portal and persisted to NVS. mDNS handles the rest — no addresses need to be kept in sync between the two sides.

---

## Repository Layout

```
burnscope/
├── README.md
├── .claude/CLAUDE.md
├── docs/
│   ├── description.md
│   ├── wire-format.md
│   ├── client-spec-v2.html        # canonical v2 client spec
│   ├── client-spec.html           # historical v1 client spec
│   ├── claude-statusline.html
│   ├── codex-app-server.html
│   ├── fsd/firmware-fsd.md
│   └── examples/
├── client/                        # per-agent collectors (Python 3.11+)
│   ├── pyproject.toml
│   └── src/burnscope_client/
│       ├── schema.py              # SessionSnapshot / AgentSnapshot
│       ├── discovery.py           # mDNS browse
│       ├── identity.py            # plaintext client_id resolver
│       ├── host_cache.py          # ~/.burnscope/ state
│       ├── pusher.py              # POST /summary, GET /health
│       ├── claude_statusline.py   # Claude Code statusline hook (per-fire)
│       ├── codex_daemon.py        # long-lived Codex daemon
│       └── cli.py                 # install/uninstall/status/pair-reset
└── firmware/                      # ESP32 (ESP-IDF), CYD only
    ├── CMakeLists.txt
    ├── sdkconfig.defaults
    └── main/
```

Single repo, two components. The wire format ([wire-format.md](./wire-format.md)) is the boundary — if Phase 2 ever needs an intermediate server, it slots in between by speaking the same `POST /summary` to the firmware.

---

## Getting Started

To be written once MVP is implementable end-to-end.


## Client (per-agent collectors)

v2 abandons v1's unified `Agent` ABC + asyncio daemon-loop in favour of one
agent-native collector per agent, each with its own lifecycle. The full
specification lives at [`client-spec-v2.html`](./client-spec-v2.html).

1. **Claude — statusline hook** (`claude_statusline.py`). Claude Code calls
   this script on every statusline fire (after each assistant message). The
   foreground process renders the line and forks a detached `--push` child
   that resolves the host (cache → mDNS), derives the plaintext identifier
   from `~/.claude.json`
   (`oauthAccount.emailAddress`, fallback `userID`), POSTs the snapshot,
   and writes `~/.burnscope/last-push.claude`.
2. **Codex — long-lived daemon** (`codex_daemon.py`). Owns a
   `codex app-server` JSON-RPC subprocess. Reader / pusher / health-
   reconciliation coroutines run together; new snapshots arrive as
   `account/rateLimits/updated` notifications and every 30 s the daemon
   compares its latest snapshot against the firmware's `/health` to detect
   drift after a reboot.

Each collector ships its plaintext identifier in `X-BurnScope-Client-Id`.
The ESP32 binds it on first push (TOFU) and rejects mismatches with `401`.
Re-pair via `burnscope-client pair-reset` (client-side cache wipe) plus the
firmware long-press BOOT (NVS slot wipe).

Shared modules: `schema.py` (frozen dataclasses matching the wire format),
`discovery.py` (mDNS browse with cache invalidation on transport error),
`identity.py` (plaintext client_id resolver), `host_cache.py` (atomic state
under `~/.burnscope/`), `pusher.py` (HTTP calls — `PushAuthError` 401 is
distinct from `PushError` transport so callers know when to invalidate the
cached host).

To add an agent: write a per-fire script (Claude-style) or a long-lived
daemon module (Codex-style) that builds `AgentSnapshot`s and uses the shared
modules. Register a `burnscope-client install <agent>` path that wires the
supervisor (`settings.json` for statusline-style, launchd/systemd for
daemon-style). No `Agent` or `Credential` base classes to extend.

The CLI (`cli.py`) is an installer/status tool, not a daemon entry. It
exposes `install/uninstall {claude,codex}`, `status`, and `pair-reset`.

## ESP32 Firmware

The "display half" of BurnScope. Holds no rolling-window state of its own — the daemon does all the math, and the firmware just renders the latest snapshot per agent and decrements a local countdown. Full spec in [fsd/firmware-fsd.md](./fsd/firmware-fsd.md); this section is the elevator pitch.

**Boot and provisioning.** On first power-up the firmware finds an empty NVS, brings up a WiFi Access Point named `BURNSCOPE-<last 4 hex of MAC>`, and serves a captive portal that scans for networks and accepts SSID + password. Credentials are persisted to NVS and the device reboots into normal STA mode. Every subsequent boot reads NVS, connects WiFi, advertises `_burnscope._tcp.local` on port 80 over mDNS, and syncs its wall clock over NTP so the countdowns are accurate. If the upstream WiFi password changes, the firmware falls back to AP mode after a handful of failed reconnect attempts so the user can re-provision without re-flashing.

**HTTP surface.** Two routes, LAN-only with TOFU pairing:

- `POST /summary` — the hot path. Validates `X-BurnScope-Client-Id` (TOFU bind on first push for that agent; `401` on mismatch). Rejects out-of-order pushes against a monotonic `captured_at` guard with `409`. Otherwise overwrites the in-RAM slot for that agent and returns `204`.
- `GET /health` — firmware version, uptime, free heap, and per known agent: bound `client_id`, `seconds_since_last_push`, and the latest `sessions` array (lets the collector detect drift after an ESP32 reboot). Requires the header to match any populated slot once any slot is bound.

Network-triggered factory reset is intentionally absent for MVP — the route had no auth and was pulled until an auth scheme exists. The BOOT-button long-press in `factory_reset.c` wipes WiFi creds *and* per-agent pairing slots and reboots into the captive portal.

**UI.** Black background, Montserrat-based proportional text in warm off-white. The header has three slots: the agent's brand mark top-left, the literal text `Usage` top-centre, and a battery slot top-right hidden on hardware variants without a battery (every variant in MVP). The body is split into two equal-height rows with rounded dark-grey backgrounds; each row carries the session's `type` rendered as a tag chip, an integer percentage, a progress bar tinted per agent, and a "resets in HH:MM:SS" countdown ticking once a second against the NTP-synced clock. When the wall clock crosses `resets_at` and no fresh push has landed yet, the bar drops to 0 automatically (the old window is logically gone). A footer band shows the bound owner identifier (truncated with an ellipsis) bottom-left and the snapshot's `updated YYYY-MM-DD HH:MM` UTC stamp bottom-right, both in a dim neutral gray so they recede below the percentage rows. When two agents are paired the device cycles between them every ~5 s. WiFi disconnect paints "Reconnecting…" on the splash and restores the agent view from the last stored snapshot as soon as IP comes back.

**Display abstraction.** Rendering depends on an abstract `display_t` interface, not on the concrete ST7789 panel driver. The MVP build registers a `cyd2usb_st7789_display` implementation; a `mock_display` exists for host unit tests. Swapping in a different panel (a bigger TFT, an e-paper variant) is meant to be a one-file change, not a renderer rewrite.

