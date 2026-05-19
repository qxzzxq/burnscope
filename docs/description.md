# BurnScope

> An always-on token & quota meter for your AI coding agents.

BurnScope tracks token usage for your AI coding agent and surfaces it on a dedicated ESP32 desk display. A daemon on your laptop reads the session data the agent already produces, computes the current window summary, discovers the ESP32 over mDNS, and pushes the rendered numbers straight to it over HTTP. No intermediate server.

## Why a hardware display

Claude Code (and most subscription-based agents) work in fixed usage windows — for Claude Code, a 5-hour rolling window. When you hit the cap and walk away, there's no good way to know when the quota refreshes without unlocking a laptop or opening a tab. An always-on display answers "how much have I used / when does it reset" at a glance.

---

## Scope

**MVP (this document):** one machine, two agents (Claude Code and Codex CLI), one board (CYD), one API endpoint. Goal is end-to-end: a real token count from a real session appears on the display and counts down to window reset. The daemon auto-detects which agents are logged in and runs every available one concurrently.

**Deferred to Phase 2:** multi-machine aggregation, additional agents (Gemini, Copilot, …), additional boards & layout families, web dashboard, auth, OTA, cost/$ estimation, persistent storage of snapshots, and an intermediate aggregation server (see note below).

> **Note on the cut server.** An earlier draft of this document put a Go aggregation server between the daemon and the ESP32. It was cut for the single-user MVP: for one laptop and one display it added two installs and a second always-on process without buying anything. It returns in Phase 2 only if it earns its keep — multi-machine aggregation, non-session agent schemas (credits, overage) that need shared state, or auth. The firmware contract (`POST /summary`) is designed to stay stable in that case: a future server simply takes the daemon's place as the thing speaking it.

---

## Architecture

```
┌──────────────────────────┐   HTTP POST /summary    ┌─────────────────────┐
│ Python daemon (laptop)   │ ──────────────────────▶ │  ESP32 (CYD)        │
│ - tails JSONL            │   AgentSnapshot JSON    │  - advertises mDNS  │
│ - computes summary       │                         │  - tiny HTTP server │
│ - discovers ESP32 (mDNS) │                         │  - renders TFT      │
└──────────────────────────┘                         └─────────────────────┘
```

- **Python daemon** — probes each supported agent's rate-limit endpoint (a tiny throwaway request whose response headers carry the usage numbers), normalises any per-agent scale (Claude returns `0.0`-`1.0` directly; Codex returns `0`-`100` integers), builds one `AgentSnapshot` per agent, and pushes each to the ESP32. Each agent declares its own `probe_interval`; a `--probe-interval` CLI flag overrides it globally. Pushes on every cycle so a freshly-booted display catches up quickly. New agents plug in by subclassing `Agent` and `Credential` — see `CLAUDE.md` for the seam.
- **ESP32 firmware** — advertises itself over mDNS as `_burnscope._tcp.local` on boot, runs a small HTTP server accepting `POST /summary`, and renders the last snapshot it received. Holds no rolling-window state of its own — the daemon does the math.
- **Discovery** — daemon uses `zeroconf` to find the advertised service. A `--esp32-host` override is accepted for networks where mDNS fails (corporate WiFi, some routers, Docker bridges).

---

## HTTP API

One endpoint, on the ESP32:

| Endpoint         | Direction        | Body                                   | Response          |
| ---------------- | ---------------- | -------------------------------------- | ----------------- |
| `POST /summary`  | daemon → ESP32   | a single `AgentSnapshot` (see [wire-format.md](./wire-format.md)) | `204 No Content`  |

Schemas are hand-written in each language (Python, C++). Schema-as-codegen is deferred until there's a third consumer.

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
├── CLAUDE.md
├── docs/
│   ├── description.md
│   ├── wire-format.md
│   └── examples/
├── client/                # Python daemon
│   ├── pyproject.toml
│   └── src/burnscope_client/
│       ├── schema.py          # wire-format dataclasses
│       ├── agent.py           # Agent ABC
│       ├── credentials.py     # Credential ABC + shared readers
│       └── agents/            # one module per supported provider
└── firmware/              # ESP32 (ESP-IDF), CYD only
    ├── CMakeLists.txt
    ├── sdkconfig.defaults
    └── main/
```

Single repo, two components. The wire format ([wire-format.md](./wire-format.md)) is the boundary — if Phase 2 ever needs an intermediate server, it slots in between by speaking the same `POST /summary` to the firmware.

---

## Getting Started

To be written once MVP is implementable end-to-end.


## Client Daemon

A long-running Python 3.11+ process that lives on the developer's laptop. It owns two jobs and keeps them in one event loop:

1. **Probe upstream agents.** Each supported agent is an `Agent` subclass that knows how to make a tiny throwaway request to its provider and parse the rate-limit headers in the response. The daemon runs one probe per agent per cycle and normalises the result into an `AgentSnapshot` matching [wire-format.md](./wire-format.md). Each agent declares its own `probe_interval` (default 120 s); a `--probe-interval` CLI flag overrides it globally.
2. **Push to the display.** Each cycle, every cached snapshot is POSTed to the ESP32. Discovery is `zeroconf`-based on the `_burnscope._tcp.local` service; a `--esp32-host` override is accepted for networks where mDNS doesn't work (corporate WiFi, some routers, Docker bridges). On a string of push failures from an mDNS-resolved host, the daemon drops the cached address and rediscovers on the next tick.

Agents are independent: each has its own probe timestamp, cached snapshot, and push-failure flag, so a Codex outage doesn't stop Claude numbers from updating, and a fresh boot of the display catches up within one tick of each running agent.

Adding a new agent is a two-class change: subclass `Credential` to teach the daemon where to find the auth blob (Keychain, file, env var) and subclass `Agent` to teach it how to probe and normalise the response. Register the class in `cli.py`'s `_AGENT_CLASSES` and it appears in `--agent` and in the auto-detect path. The wire format stays the same; only the per-provider scale (Claude already returns `0.0`–`1.0`; Codex returns `0`–`100` integers) needs normalising.

The CLI has no mandatory flags — `burnscope-client` with no arguments auto-detects every agent whose credentials are present and runs them all concurrently. `--agent` is repeatable for explicit selection.

## ESP32 Firmware

The "display half" of BurnScope. Holds no rolling-window state of its own — the daemon does all the math, and the firmware just renders the latest snapshot per agent and decrements a local countdown. Full spec in [fsd/firmware-fsd.md](./fsd/firmware-fsd.md); this section is the elevator pitch.

**Boot and provisioning.** On first power-up the firmware finds an empty NVS, brings up a WiFi Access Point named `BURNSCOPE-<last 4 hex of MAC>`, and serves a captive portal that scans for networks and accepts SSID + password. Credentials are persisted to NVS and the device reboots into normal STA mode. Every subsequent boot reads NVS, connects WiFi, advertises `_burnscope._tcp.local` on port 80 over mDNS, and syncs its wall clock over NTP so the countdowns are accurate. If the upstream WiFi password changes, the firmware falls back to AP mode after a handful of failed reconnect attempts so the user can re-provision without re-flashing.

**HTTP surface.** Three routes, no auth (LAN trust):

- `POST /summary` — the hot path. Accepts one `AgentSnapshot`, overwrites the in-RAM cache for that agent, marks the UI dirty, returns `204`.
- `GET /health` — firmware version, uptime, free heap, and `seconds_since_last_push` per known agent. Useful for the daemon and for poking at the device from `curl`.
- `POST /factory-reset` — erases NVS and reboots into the captive portal. The same effect as long-pressing the boot button.

**UI.** Black background, white-on-black monospaced text (with light-grey accents permitted). The header has three slots: the agent's logo top-left, the literal text `USAGE` top-centre, and a battery icon top-right that is hidden on hardware variants without a battery — which is every variant in MVP, since the CYD draws power from its USB-C or micro-USB port. The body is split into two equal-height rows with rounded dark-grey backgrounds; each row carries a progress bar, the integer percentage, the session's `type` string rendered verbatim as a tag (so Claude's `current`/`weekly` and Codex's `primary`/`secondary` both work without firmware changes), and a "resets in HH:MM:SS" countdown ticking once a second against the NTP-synced clock. If no snapshot arrives for ~60 s the rows dim to flag staleness.

**Display abstraction.** Rendering depends on an abstract `display_t` interface, not on the concrete ST7789 panel driver. The MVP build registers a `cyd2usb_st7789_display` implementation; a `mock_display` exists for host unit tests. Swapping in a different panel (a bigger TFT, an e-paper variant) is meant to be a one-file change, not a renderer rewrite.

