# BurnScope

> An always-on token & quota meter for your AI coding agents.

BurnScope tracks token usage for your AI coding agent and surfaces it on a dedicated ESP32 desk display. A daemon reads the session data the agent already produces and pushes usage events to a local server, which keeps a running window total and exposes a small HTTP API. The ESP32 polls that API.

## Why a hardware display

Claude Code (and most subscription-based agents) work in fixed usage windows — for Claude Code, a 5-hour rolling window. When you hit the cap and walk away, there's no good way to know when the quota refreshes without unlocking a laptop or opening a tab. An always-on display answers "how much have I used / when does it reset" at a glance.

---

## Scope

**MVP (this document):** one machine, one agent (Claude Code), one board (CYD), two API endpoints. Goal is end-to-end: a real token count from a real session appears on the display and counts down to window reset.

**Deferred to Phase 2:** multi-machine aggregation, additional agents (Codex, Gemini, Copilot, …), additional boards & layout families, web dashboard, auth, mDNS discovery, captive-portal Wi-Fi provisioning, OTA, cost/$ estimation, persistent storage. See [TODO.md](./TODO.md).

---

## Architecture

```
┌─────────────────┐  HTTP POST  ┌──────────────────┐  HTTP GET   ┌─────────────┐
│ Client daemon   │ ──────────▶ │      Server      │ ◀────────── │   ESP32     │
│ (laptop)        │   (push)    │ rolling window   │   (poll)    │  (CYD)      │
└─────────────────┘             │ + HTTP API       │             └─────────────┘
                                └──────────────────┘
```

- **Client daemon** — tails `~/.claude/projects/**/*.jsonl`, extracts token counts, POSTs to the server.
- **Server** — keeps the current 5-hour window total in memory; serves the read API.
- **ESP32 firmware** — polls the API every few seconds; renders tokens used and time until reset.

For MVP the daemon and server can run on the same machine. They are still separate processes — splitting later (to a always-on server) doesn't require a refactor.

---

## HTTP API

| Endpoint                  | Returns                                        |
| ------------------------- | ---------------------------------------------- |
| `POST /api/events`        | Push usage events from the daemon              |
| `GET /api/summary/session` | Latest session snapshots (5h/7d for Claude, primary/secondary for Codex) for each agent |

Schemas are hand-written in each language (Python, Go, C++). Schema-as-codegen is deferred until there's a third consumer.

---

## Tech Stack

| Component       | Choice                                          |
| --------------- | ----------------------------------------------- |
| Firmware        | PlatformIO + Arduino, C++, TFT_eSPI             |
| Board           | Cheap Yellow Display (ESP32-2432S028R), 320×240 |
| Server          | Go (`net/http`)                                 |
| Client daemon   | Python 3.11+ (`watchdog`, `httpx`)              |

WiFi credentials and the server URL are compiled into the firmware for MVP. Network provisioning UX is Phase 2.

---

## Repository Layout

```
burnscope/
├── README.md
├── CLAUDE.md
├── docs/
│   ├── description.md
│   └── TODO.md
├── client/                # Python daemon
│   ├── pyproject.toml
│   └── burnscope_client/
├── server/                # Go server + API
│   ├── go.mod
│   └── cmd/burnscope-server/
└── firmware/              # ESP32 (PlatformIO), CYD only
    ├── platformio.ini
    └── src/
```

Single repo. Component boundaries kept clean so a split is possible later.

---

## Development Workflow

Flashing and runtime testing happen against a remote CYD attached to a Raspberry Pi, which exposes the ESP32's serial port over the network via `esp_rfc2217_server`. Any developer (or Claude) can flash and monitor without physical access.

```ini
# platformio.ini
upload_port = rfc2217://workbench.local:4000?ign_set_control
```

```bash
miniterm.py rfc2217://workbench.local:4000?ign_set_control 115200
```

---

## Getting Started

To be written once MVP is implementable end-to-end. See [TODO.md](./TODO.md).
