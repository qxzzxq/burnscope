# BurnScope

> An always-on token & quota meter for your AI coding agents.

BurnScope tracks token usage for your AI coding agent and surfaces it on a dedicated ESP32 desk display. A daemon on your laptop reads the session data the agent already produces, computes the current window summary, discovers the ESP32 over mDNS, and pushes the rendered numbers straight to it over HTTP. No intermediate server.

## Why a hardware display

Claude Code (and most subscription-based agents) work in fixed usage windows — for Claude Code, a 5-hour rolling window. When you hit the cap and walk away, there's no good way to know when the quota refreshes without unlocking a laptop or opening a tab. An always-on display answers "how much have I used / when does it reset" at a glance.

---

## Scope

**MVP (this document):** one machine, two agents (Claude Code and Codex CLI), one board (CYD), one API endpoint. Goal is end-to-end: a real token count from a real session appears on the display and counts down to window reset. The daemon auto-detects which agents are logged in and runs every available one concurrently.

**Deferred to Phase 2:** multi-machine aggregation, additional agents (Gemini, Copilot, …), additional boards & layout families, web dashboard, auth, captive-portal Wi-Fi provisioning, OTA, cost/$ estimation, persistent storage, and an intermediate aggregation server (see note below).

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

- **Python daemon** — probes each supported agent's rate-limit endpoint (a tiny throwaway request whose response headers carry the usage numbers), normalises any per-agent scale (Claude returns `0.0`-`1.0` directly; Codex returns `0`-`100` integers), builds one `AgentSnapshot` per agent, and pushes each to the ESP32. Also tails `~/.claude/projects/**/*.jsonl` as a cheap activity signal that switches probes between an active and an idle cadence. Pushes on every cycle so a freshly-booted display catches up quickly. New agents plug in by subclassing `Agent` and `Credential` — see `CLAUDE.md` for the seam.
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
| Daemon          | Python 3.11+ (`watchdog`, `httpx`, `zeroconf`)                      |

WiFi credentials are compiled into the firmware for MVP. mDNS handles the rest — no addresses need to be kept in sync between the two sides. Captive-portal network provisioning is Phase 2.

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
