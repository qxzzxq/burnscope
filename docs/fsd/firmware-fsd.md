# BurnScope ESP32 Firmware — Functional Specification Document (FSD)

> **Scope:** Firmware for the Cheap Yellow Display (CYD) that displays a
> BurnScope `AgentSnapshot` pushed over HTTP by the Python daemon. MVP only.
> Multi-machine aggregation, OTA, auth, and additional boards are Phase 2 —
> not in this document.

> **Inputs to this FSD:** `docs/description.md` (§ ESP32 Firmware) and
> `docs/wire-format.md` (the daemon ↔ firmware contract). The wire format
> takes precedence wherever the description is vague.

---

## 1. System Overview

### 1.1 Purpose

BurnScope is an always-on desk meter for AI-coding-agent token quotas. The
firmware is the "display half" of a two-process system: a Python daemon on
the user's laptop probes one or more agents (Claude Code, Codex CLI) for
their rate-limit headers, normalises the result into an `AgentSnapshot`,
discovers the firmware over mDNS, and POSTs the snapshot to it. The
firmware renders the latest snapshot per agent on the CYD's 320×240 panel
and locally counts down "time until window reset".

### 1.2 Problem Statement

Subscription-based coding agents (notably Claude Code) operate against a
fixed rolling-window quota — 5 h for Claude Code, plus a 7-day envelope.
When a developer hits the cap, there is no glanceable way to know "how
much have I burned" or "when does it reset" without unlocking a laptop or
opening a browser tab. A dedicated always-on display answers both at a
glance.

### 1.3 Users / Stakeholders

- **End user (developer):** sees the panel from their peripheral vision,
  expects it to "just work" after a one-time setup.
- **Daemon process:** the only entity speaking to the firmware. Trusted
  LAN peer; no auth in MVP.
- **Future contributor:** must be able to swap the panel (e.g. for a
  bigger TFT or e-paper variant) without rewriting the rendering layer —
  hence the display-abstraction requirement (FR-5).

### 1.4 Goals & Non-Goals

**Goals (MVP):**

- Boot from cold, provision WiFi via captive portal on first run, then
  reconnect autonomously on every subsequent boot.
- Advertise itself over mDNS so the daemon needs zero configuration.
- Accept `POST /summary` and render the result within a budget that feels
  immediate to the user.
- Render whatever `sessions[].type` strings arrive, so the same firmware
  works for Claude Code's `5h`/`7d` and Codex CLI's `primary`/`secondary`.
- Be stable enough to leave running for weeks (always-on watchdog).

**Non-goals (deferred to Phase 2 or out of scope):**

- OTA firmware updates.
- Authentication on `POST /summary` (LAN-trust only).
- Persisting snapshots across reboots (RAM-only — the daemon re-pushes
  within ~30 s anyway, per `wire-format.md`).
- Aggregating across multiple machines.
- Rendering a third quota bucket (overage / credits) — schema allows it
  but the MVP layout only has room for two rows.
- Battery operation — the CYD has no battery; the battery icon in the
  header (described in `description.md`) is a stub for hardware variants
  that have one.

### 1.5 High-Level System Flow

```
Power-on
   │
   ├─► NVS has WiFi creds? ──no──► AP mode + captive portal ──┐
   │                                                          │
   │                          credentials saved + reboot ◄────┘
   │                                yes
   ▼
STA connect → mDNS advertise (_burnscope._tcp) + NTP sync
   │
   ├──◄ POST /summary  (one AgentSnapshot)            from daemon
   │       │
   │       ▼
   │   parse → store per-agent snapshot → repaint UI
   │
   └──◄ GET /health    (firmware version / uptime / last-snapshot-age)
```

---

## 2. System Architecture

### 2.1 Logical Architecture

Subsystems, runtime-only:

| Subsystem            | Responsibility |
|----------------------|----------------|
| **Boot orchestrator**| Initialises NVS, panel, LVGL; decides STA vs AP mode based on stored credentials. |
| **WiFi manager**     | STA connect, reconnect, fall-back to AP after `N` consecutive auth failures. |
| **Captive portal**   | AP (`BURNSCOPE-XXXX`), DNS hijack, HTTP form for SSID/password, write to NVS, reboot. |
| **mDNS responder**   | Advertises `_burnscope._tcp.local` on port 80 with a TXT record carrying firmware version. |
| **NTP client**       | Syncs wall clock at boot and every 6 h. Used for "X s ago" and "resets in Y" math. |
| **HTTP server**      | Two routes: `POST /summary`, `GET /health`. No middleware, no auth. |
| **Snapshot store**   | RAM-only `map<agent, AgentSnapshot>` (max two agents in MVP — `claude`, `codex`). |
| **Renderer**         | LVGL repaint on snapshot change *and* once a second for countdowns. Uses the **Display** abstraction. |
| **Display abstraction** | `display_t` virtual interface; concrete `cyd2usb_st7789_display` for the MVP panel. |
| **Watchdog**         | Software task-heartbeat WDT plus IDF Task Watchdog (TWDT). |

Data flow on the hot path is a straight line: HTTP → parser → snapshot
store → UI dirty flag → next LVGL tick repaints. No queues, no tasks
fighting for locks beyond the LVGL port mutex.

### 2.2 Hardware / Platform Architecture

- **Board:** Cheap Yellow Display, `cyd2usb` variant (one USB-C and one
  micro-USB port — either can power the board and expose the serial
  console). ESP32-WROOM-32, 4 MB flash, 520 KiB SRAM. Reference pinout
  per the ESP32-Cheap-Yellow-Display project.
- **Panel:** ST7789, 320×240 landscape (native 240×320 portrait, rotated
  in LVGL). BGR pixel order, inversion off, 16-bit RGB565. SPI bus at
  20 MHz (40 MHz produces bit errors on the non-IOMUX pins — confirmed
  empirically in `firmware/main/main.c`).
- **Backlight:** GPIO21, active-high.
- **Touch:** the panel has a resistive touch controller but **MVP does
  not use touch** — provisioning happens from another device via the AP.
- **Power:** 5 V via either the USB-C or the micro-USB port. No battery
  in this hardware revision.
- **Connectivity:** 2.4 GHz WiFi only (ESP32 single-band).

### 2.3 Software Architecture

- **SDK:** ESP-IDF v6.x (locked at 6.0.1 via `dependencies.lock`).
- **UI library:** LVGL 9.5 via `espressif/esp_lvgl_port` 2.8.
- **Display driver:** ESP-IDF built-in `esp_lcd` with ST7789 panel ops.
- **Networking:** IDF's `esp_wifi`, `esp_netif`, `esp_http_server`,
  `mdns`, `esp_sntp`.
- **Persistence:** `nvs_flash` partition (6 KiB at 0x9000 per
  `partitions-4mb.csv`). Stores WiFi credentials only.
- **Partition layout:** dual OTA app partitions are already reserved
  in `partitions-4mb.csv` so a future Phase 2 OTA flow can land without
  re-partitioning — but no OTA logic ships in MVP.

**Boot sequence (warm boot, NVS has creds):**

1. NVS init → read SSID/password.
2. Panel init → backlight on → LVGL init → render "Connecting…" splash.
3. WiFi STA connect with stored creds (`WIFI_AUTH_WPA2_PSK` minimum).
4. On IP acquired: start mDNS responder, kick SNTP, bind HTTP server on
   port 80.
5. Renderer transitions to the "waiting for first snapshot" screen.

**Boot sequence (cold boot, empty NVS):**

1. NVS init → no creds.
2. Panel init → render "Setup mode — connect to `BURNSCOPE-XXXX`".
3. Start AP `BURNSCOPE-<last 4 of MAC>` (open auth, channel 1).
4. Start captive-portal DNS hijack + HTTP form.
5. On form submit → write creds to NVS → `esp_restart()`.

**Tasks (FreeRTOS):**

| Task | Stack | Priority | Notes |
|------|-------|----------|-------|
| `app_main` (LVGL) | 8 KiB | 1 | Drives LVGL tick; idle most of the time. |
| HTTP server      | IDF default | 5 | Handles `POST /summary` and `GET /health`. |
| WiFi event group | IDF default | 18 | Owned by IDF; we hook events only. |
| Watchdog task    | 2 KiB | 4 | Heartbeats from each long-lived task. |

---

## 3. Implementation Phases

### 3.1 Phase 1 — Bring-up & Networking

**Scope:** boot, panel, WiFi STA from compiled defaults *(temporary —
swapped for captive portal in Phase 2)*, mDNS advertise, NTP, HTTP
skeleton (returns 204 but stores nothing), watchdog.

**Deliverables:**

- Reproducible IDF build (`idf.py build flash monitor`).
- "Hello, world" replaced with a placeholder "Waiting for daemon…"
  splash that survives WiFi reconnects.
- `dns-sd -B _burnscope._tcp` on macOS finds the device.
- `curl -X POST http://burnscope.local/summary -d '{}'` returns 204.

**Exit criteria:**

- WIFI-001, WIFI-003 pass.
- TC-MDNS-100 passes.
- TC-HTTP-100 (skeleton response) passes.
- WDT-001 + TC-WDT-100 pass.

**Dependencies:** none beyond the existing hello-world skeleton.

### 3.2 Phase 2 — Provisioning & Snapshot Rendering

**Scope:** captive-portal AP mode with credential capture and NVS
persistence; full UI per the description (header + two rounded rows);
real `POST /summary` parsing + per-agent store + repaint; countdown timer
driven by NTP-synced clock; `GET /health` shape; factory-reset path.

**Deliverables:**

- First boot opens captive portal, accepts credentials, persists to NVS,
  comes back up in STA mode.
- A real `AgentSnapshot` (Claude or Codex) rendered as two progress-bar
  rows with the `type` string as the tag and a `used_pct × 100` percentage
  rounded to nearest integer.
- "Resets in HH:MM:SS" countdown ticks every second locally.

**Exit criteria:**

- All AP-* and CP-* tests pass.
- All NVS-* tests pass.
- TC-SUM-100, TC-SUM-101, TC-SUM-102 pass.
- TC-UI-100, TC-UI-101 pass.

**Dependencies:** Phase 1 complete.

### 3.3 Phase 3 — Hardening (still MVP)

**Scope:** stability and recovery — watchdog tuning, NVS corruption
fall-back, AP-mode fall-back after persistent STA auth failure, log
hygiene, display-abstraction substitutability proof (a `MockDisplay`
unit-testable on the host).

**Deliverables:**

- 24 h soak test with periodic `POST /summary` and intermittent WiFi
  flapping — no reboots, no leaks (free heap stable ± 5 %).
- All edge-case tests in § 8 pass.

**Exit criteria:**

- EC-100, EC-101, EC-110, EC-115 pass.
- EC-NVS-200, EC-NVS-202 pass.
- EC-CP-200 passes.
- WDT-005 demonstrably true under flapping.

**Dependencies:** Phase 2 complete.

> **Phase 4+ — out of scope for this FSD:** OTA, push auth, battery
> support, additional panels, multi-machine aggregation. Tracked in
> `docs/description.md` § Scope.

---

## 4. Functional Requirements

### 4.1 Functional Requirements (FR)

**FR-1 Boot & Provisioning**

- **FR-1.1** [Must]: On boot, the firmware shall read WiFi credentials
  from NVS and attempt STA connection if credentials are present.
- **FR-1.2** [Must]: When no valid WiFi credentials exist in NVS, the
  firmware shall start a WiFi Access Point and a captive portal for
  first-boot provisioning.
- **FR-1.3** [Must]: The AP SSID shall follow the pattern
  `BURNSCOPE-<last 4 hex digits of MAC>` to avoid collisions between
  devices on the same network.
- **FR-1.4** [Must]: The captive portal shall present a form for SSID
  and password entry and shall list scanned 2.4 GHz networks with their
  RSSI.
- **FR-1.5** [Must]: On successful credential submission, the firmware
  shall persist them to NVS and reboot into STA mode.
- **FR-1.6** [Should]: After N consecutive STA auth failures (default
  `N = 5`), the firmware shall fall back to AP + captive-portal mode so
  the user can re-provision without re-flashing.
- **FR-1.7** [Should]: A factory-reset trigger (long-press of the boot
  button for ≥ 5 s, or `POST /factory-reset`) shall erase NVS and reboot
  into AP mode.

**FR-2 Network Services**

- **FR-2.1** [Must]: Once STA-connected, the firmware shall advertise
  service `_burnscope._tcp.local` over mDNS on port 80.
- **FR-2.2** [Should]: The mDNS TXT record shall include
  `version=<firmware-version>` so the daemon can warn on mismatch.
- **FR-2.3** [Must]: The firmware shall synchronise its wall clock via
  SNTP (default pool: `pool.ntp.org`) within 30 s of acquiring an IP and
  shall resync every 6 h thereafter.
- **FR-2.4** [Should]: On WiFi disconnect, the firmware shall attempt
  STA reconnection with exponential backoff (1 s → 30 s cap).

**FR-3 HTTP Server**

- **FR-3.1** [Must]: The firmware shall expose `POST /summary` on
  port 80, accept a single `AgentSnapshot` JSON body per the schema in
  `docs/wire-format.md`, and respond with `204 No Content` on success.
- **FR-3.2** [Must]: `POST /summary` shall respond with `400 Bad Request`
  for any of: invalid JSON, missing required field, `agent` not in the
  recognised set, `used_pct` outside `[0.0, 1.0]`.
- **FR-3.3** [Must]: The firmware shall keep the latest `AgentSnapshot`
  per `agent` value in RAM and shall not persist snapshots across reboots.
- **FR-3.4** [Should]: `GET /health` shall return JSON containing
  `firmware_version`, `uptime_s`, `free_heap_b`, and `seconds_since_last_push`
  (per known agent).
- **FR-3.5** [May]: `POST /factory-reset` shall erase NVS and reboot
  (provides a remote alternative to the button hold).

**FR-4 Rendering**

- **FR-4.1** [Must]: The display shall paint a black background. Text
  shall be white with light-grey accents permitted.
- **FR-4.2** [Must]: The text font shall be monospaced; a Material-Design
  monospaced face is preferred (assumed: Roboto Mono shipped via LVGL).
- **FR-4.3** [Must]: The header shall display, left-to-right:
  the current agent's logo (top-left, ~32 px square), the literal text
  `USAGE` (top-centre), and a battery icon (top-right) which is hidden
  when the hardware reports no battery.
- **FR-4.4** [Must]: The body shall be divided into two equal-height
  rows separated by a small gap, each with a dark-grey rounded-rectangle
  background.
- **FR-4.5** [Must]: Each body row shall display a horizontal progress
  bar (filled proportionally to `used_pct`), the integer percentage
  (`round(used_pct × 100)` followed by `%`), and the session's `type`
  string rendered verbatim as a tag.
- **FR-4.6** [Must]: When a `POST /summary` arrives, the firmware shall
  re-render the affected agent's view within 100 ms.
- **FR-4.7** [Should]: The two progress bars shall use distinct accent
  colours so the rows are visually distinguishable; the exact palette is
  an implementation detail.
- **FR-4.8** [Should]: A countdown derived from `resets_at − now()` shall
  be displayed inside each row and updated at least once per second.
- **FR-4.9** [Should]: If no snapshot has been received within
  `2 × keepalive` (default keepalive ≈ 30 s per `wire-format.md`), the
  firmware shall visually mark the data as "stale" (e.g. dimmed bars).
- **FR-4.10** [May]: When multiple agents have pushed snapshots, the
  firmware shall cycle between agents on a slow timer (default 5 s).
  When only one agent has pushed, that view shall be permanent.

**FR-5 Display Abstraction**

- **FR-5.1** [Must]: The rendering layer shall depend on an abstract
  `display_t` interface exposing `init`, `flush`, and `info` operations,
  not on a concrete panel driver.
- **FR-5.2** [Must]: A concrete `cyd2usb_st7789_display` implementation
  shall be the only display registered in the MVP build.
- **FR-5.3** [Should]: A `mock_display` implementation usable in host
  unit tests shall exist to prove the rendering layer is substitutable.

**FR-6 Persistence**

- **FR-6.1** [Must]: WiFi credentials (SSID, password) shall be the only
  values persisted to NVS in MVP.
- **FR-6.2** [Must]: Credentials shall be read once at boot and never
  again echoed to logs or to the `GET /health` response.

### 4.2 Non-Functional Requirements (NFR)

- **NFR-1.1** [Must]: Warm-boot (creds in NVS) time from reset to
  "ready for first POST" shall be ≤ 5 s on a well-known WiFi network.
- **NFR-1.2** [Should]: First-boot (captive-portal) time from reset to
  AP visible to a phone shall be ≤ 10 s.
- **NFR-2.1** [Must]: End-to-end latency from receipt of `POST /summary`
  to repainted pixels shall be ≤ 200 ms (LAN, WPA2, 2.4 GHz).
- **NFR-2.2** [Should]: HTTP server shall accept request bodies up to
  4 KiB; bodies larger than 16 KiB shall be rejected with `413`.
- **NFR-3.1** [Must]: Free heap at steady state shall remain ≥ 64 KiB
  for ≥ 24 h of normal operation. A measurable downward drift over the
  soak window is a failure.
- **NFR-3.2** [Must]: The IDF Task Watchdog (TWDT) shall reboot the
  device within 10 s of any task ceasing to feed it.
- **NFR-4.1** [Should]: Logs shall use `ESP_LOG` at `INFO` by default
  and `ERROR` for production builds; default tag is the subsystem name
  (`wifi`, `mdns`, `http`, `render`, `nvs`).
- **NFR-5.1** [Should]: The wall clock shall stay within ±2 s of UTC
  outside the brief windows immediately after boot but before the first
  SNTP response.

### 4.3 Constraints

- **C-1:** ESP32-WROOM-32 has no native USB; serial logging is via the
  CYD's onboard CP2102N over either the USB-C or the micro-USB port.
- **C-2:** SPI to the display is **not** routed on IOMUX pins on the CYD,
  capping reliable pclk at ~20 MHz (per `firmware/main/main.c`).
- **C-3:** ESP32 supports **2.4 GHz only**. A 5 GHz-only network at the
  user site is unsupported.
- **C-4:** ESP-IDF v6.x and LVGL ≥ 8 < 10 (locked at 9.5) per
  `firmware/dependencies.lock`.
- **C-5:** Flash budget is the dual-OTA partition of `0x1E0000`
  (~1.9 MiB) per slot. The MVP currently uses one slot; the second is
  reserved for Phase 2 OTA.

---

## 5. Risks, Assumptions & Dependencies

### 5.1 Technical Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| 2.4 GHz congestion / weak signal at the user's desk causes frequent reconnects | Medium | Low — render thread independent | Reconnect backoff + "stale" visual marker (FR-4.9) |
| ST7789 SPI bit errors at higher clocks | Low (already mitigated) | Medium — visible glitches | Pinned to 20 MHz per known-good config (C-2) |
| mDNS unreliable on some routers (Docker bridges, corporate WiFi) | Medium | Medium — daemon can't find device | Daemon already supports `--esp32-host` override (`docs/description.md` § Architecture) |
| NVS partition fills if writes ever loop | Very low (creds written once) | High — provisioning broken | Encapsulate writes in a helper that refuses to rewrite identical creds |
| Captive portal phones-home behaviour varies (iOS vs Android) | Medium | Low — user sees portal late | Reply to common probe URLs (Apple `/library/test/success.html`, Android `/generate_204`) with the portal HTML |
| LVGL ABI churn between minor versions | Low (locked to 9.5) | High if unlocked | Version pin in `dependencies.lock`; document the upgrade path |

### 5.2 Assumptions

- **A-1** (assumed): The user's network uses WPA2-PSK or WPA3-PSK.
  Enterprise / 802.1X is out of scope.
- **A-2** (assumed): The user's network permits mDNS (UDP 5353). If
  not, the user falls back to the daemon's `--esp32-host` override.
- **A-3** (assumed): The Material-Design monospaced font referenced in
  `docs/description.md` will be supplied as a built-in LVGL font
  (`Roboto Mono` or similar). The exact face is an implementation
  decision, not a requirement.
- **A-4** (assumed): The "battery icon if device is battery powered"
  requirement (`docs/description.md`) refers to a future hardware
  variant; the current CYD has no battery so the icon is always hidden
  in MVP.
- **A-5** (assumed): The keepalive interval used to detect "stale"
  in FR-4.9 is the daemon's ~30 s figure from `docs/wire-format.md`.

### 5.3 Documentation conflict to resolve

`docs/description.md` § Tech Stack currently reads *"WiFi credentials are
compiled into the firmware for MVP. mDNS handles the rest — no addresses
need to be kept in sync between the two sides. Captive-portal network
provisioning is Phase 2."* This contradicts § ESP32 Firmware in the same
document, which specifies captive-portal provisioning as the first-boot
flow. **This FSD codifies the captive-portal flow** (per § ESP32
Firmware) and a follow-up edit to `docs/description.md` § Tech Stack is
needed to remove the stale paragraph.

### 5.4 External Dependencies

- ESP-IDF v6.0.1 (`dependencies.lock`).
- `espressif/esp_lvgl_port` 2.8.x (`dependencies.lock`).
- `lvgl/lvgl` 9.5.x (`dependencies.lock`).
- Public NTP infrastructure (`pool.ntp.org` by default).

---

## 6. Interface Specifications

### 6.1 External Interfaces

#### 6.1.1 `POST /summary` (daemon → firmware)

- Transport: HTTP/1.1 on TCP 80, no TLS, no auth.
- Request: `Content-Type: application/json`, body is one `AgentSnapshot`.
- Success response: `204 No Content`, empty body.
- Error responses: `400 Bad Request` (bad JSON / schema / range), `413
  Payload Too Large` (> 16 KiB), `405 Method Not Allowed` (non-POST).
- Idempotency: each request overwrites the previous snapshot for that
  `agent` key. The firmware keeps no history.

#### 6.1.2 `GET /health` (daemon → firmware)

- Transport: HTTP/1.1 on TCP 80, no auth.
- Response: `200 OK`, `Content-Type: application/json`. Example:

  ```json
  {
    "firmware_version": "0.1.0",
    "uptime_s": 12345,
    "free_heap_b": 134000,
    "agents": {
      "claude": { "seconds_since_last_push": 12 },
      "codex":  { "seconds_since_last_push": 31 }
    }
  }
  ```

#### 6.1.3 `POST /factory-reset` (daemon or admin → firmware)

- Erases NVS and reboots. Response: `202 Accepted` (then reboot).

#### 6.1.4 mDNS service advertisement

- Service: `_burnscope._tcp.local`.
- Port: 80.
- TXT record: `version=<firmware-version>`.
- Hostname: `burnscope-<last 4 hex of MAC>.local` (so multiple devices
  on one LAN don't collide).

#### 6.1.5 Captive portal HTTP (AP mode only, 192.168.4.1)

- `GET /` → provisioning HTML form.
- Catch-all 302 redirect on AP-mode HTTP requests to `/`.
- `GET /generate_204` (Android probe) → 200 with portal HTML
  (forces the OS to open the portal automatically).
- `GET /hotspot-detect.html` and `/library/test/success.html` (Apple
  probes) → 200 with portal HTML.
- `POST /provision` → form-encoded `ssid` + `password`; on success
  responds 200 ("Saved — rebooting…") and triggers `esp_restart()` after
  500 ms.

#### 6.1.6 User Interface (firmware → user)

See FR-4. Sketch:

```
┌──────────────────────────────────────────────┐
│ [logo]       USAGE                  [batt]   │ ← header
├──────────────────────────────────────────────┤
│ ╭──────────────────────────────────────────╮ │
│ │ 5h                                   63% │ │ ← type · remaining
│ │ █████████████████████████░░░░░░░░░░░░░░░ │ │ ← progress bar
│ │ resets in 2h 12m                         │ │ ← countdown
│ ╰──────────────────────────────────────────╯ │
│ ╭──────────────────────────────────────────╮ │
│ │ 7d                                   82% │ │
│ │ █████████████████████████████████░░░░░░░ │ │
│ │ resets in 4d 05h                         │ │
│ ╰──────────────────────────────────────────╯ │
└──────────────────────────────────────────────┘
```

Per-row layout:

1. **Header sub-row** — session `type` (from `SessionSnapshot.type`,
   verbatim) on the left; remaining percentage (`100 − used_pct`,
   rounded) on the right.
2. **Bar sub-row** — horizontal progress bar whose filled portion
   represents remaining capacity, so the bar and the percentage track
   together (full bar = lots of headroom; empty bar = burned through).
3. **Countdown sub-row** — `resets in Xh XXm` when the remaining time
   to `resets_at` is < 24 h; otherwise `resets in Xd XXh`. The minute /
   hour field is zero-padded to two digits.

### 6.2 Internal Interfaces

- `display_t` — abstract panel: `init(rotation)`, `flush(area, pixels)`,
  `info()` returning `{ width, height, has_battery }`.
- `snapshot_store` — `put(AgentSnapshot)`, `get(agent_id)`,
  `seconds_since_last_push(agent_id)`. Backed by a tiny statically-sized
  map keyed by the `agent` string.
- `wifi_state_t` — `{ DISCONNECTED, CONNECTING, CONNECTED, AP_MODE }`,
  consumed by the renderer to draw the "connecting" splash vs. the
  normal view.

### 6.3 Data Models / Schemas

Single message type, mirrored from `docs/wire-format.md` and pinned here
for the firmware's benefit. Source of truth is the wire-format doc; this
table must match it.

**`AgentSnapshot`** (request body of `POST /summary`):

| Field         | Type             | Notes |
|---------------|------------------|-------|
| `agent`       | `"claude"` \| `"codex"` | Enum. Unknown values → 400. |
| `captured_at` | `int` (unix s, UTC) | Used for "last update Xs ago". |
| `sessions`    | array of `SessionSnapshot` | Order not guaranteed; firmware looks up by `type`. |

**`SessionSnapshot`** (element of `sessions[]`):

| Field       | Type            | Notes |
|-------------|-----------------|-------|
| `type`      | `string`        | Agent's own vocabulary; rendered verbatim. |
| `used_pct`  | `float` 0.0–1.0 | Outside range → 400. |
| `resets_at` | `int` (unix s)  | Firmware computes `resets_at − now()` locally. |

### 6.4 Commands / Opcodes

Not applicable — JSON over HTTP, no custom opcodes.

---

## 7. Operational Procedures

### 7.1 Deployment / Flashing

```bash
cd firmware
idf.py set-target esp32
idf.py build
idf.py -p <PORT> flash monitor
```

A successful flash + boot yields the "Setup mode — connect to
`BURNSCOPE-XXXX`" splash on first boot.

### 7.2 First-time Provisioning

1. Power on device — AP `BURNSCOPE-<XXXX>` becomes visible.
2. Phone or laptop connects to the AP (open auth).
3. Captive-portal prompt opens automatically (Android / iOS will probe
   `generate_204` / `hotspot-detect.html`, both of which the firmware
   intercepts).
4. Select home WiFi from the scanned list, enter password.
5. Submit → firmware writes NVS and reboots.
6. Device comes up in STA mode and is discoverable as
   `burnscope-<XXXX>.local`.

### 7.3 Normal Operation

- The daemon discovers the device via mDNS and sends `POST /summary` on
  every JSONL change plus a ~30 s keepalive.
- The device renders the latest per-agent snapshot continuously. The
  countdown timer ticks locally — no traffic needed between pushes.
- If no snapshot arrives within `2 × keepalive`, the UI dims to indicate
  staleness (FR-4.9).

### 7.4 Maintenance

- **Updating WiFi credentials:** factory-reset (button hold or
  `POST /factory-reset`) → re-provision via captive portal.
- **Firmware upgrade:** USB re-flash (`idf.py flash`) in MVP. OTA is
  Phase 2.
- **Free-heap monitoring:** `GET /health` returns `free_heap_b`; a drop
  below 64 KiB at steady state should be investigated (NFR-3.1).

### 7.5 Recovery

- **STA connect repeatedly fails (e.g. WiFi password changed
  upstream):** after `N = 5` consecutive auth failures, firmware falls
  back to AP + captive-portal mode (FR-1.6).
- **NVS corruption:** firmware falls back to AP + portal (NVS-001-class
  fallback, see EC-NVS-200).
- **Watchdog reset:** the TWDT will reboot the device if any task hangs.
  Boot reason is logged on the next start (`rst:0xc`).

---

## 8. Verification & Validation

### 8.1 Phase 1 Verification

| Test ID    | Feature                  | Procedure                                                                                              | Success Criteria |
|------------|--------------------------|--------------------------------------------------------------------------------------------------------|------------------|
| WIFI-001   | STA connect              | Pre-load NVS with valid creds, boot, observe IP acquisition.                                           | IP assigned, `wifi` log shows `IP_EVENT_STA_GOT_IP`. |
| WIFI-003   | STA reconnect            | Disable AP, wait 30 s, re-enable AP.                                                                   | Device reconnects automatically without reboot. |
| WIFI-005   | WPA2/WPA3 auth           | Connect to a WPA2-PSK network and a WPA3-PSK network in sequence.                                      | Both succeed. |
| TC-MDNS-100| mDNS advertise           | Run `dns-sd -B _burnscope._tcp` on a peer; verify service and resolve hostname.                        | Service visible, resolves to device IP. |
| TC-NTP-100 | SNTP sync                | After boot, `GET /health`; verify `uptime_s` increments and (via logs) `sntp_get_sync_status == COMPLETED` within 30 s of IP. | Sync completes ≤ 30 s after IP. |
| TC-HTTP-100| HTTP skeleton            | `curl -X POST http://burnscope.local/summary -d '{...valid AgentSnapshot...}'`                          | 204 returned. |
| TC-WDT-100 | Software watchdog        | Inject a `vTaskDelay(portMAX_DELAY)` in a heartbeat task (test build).                                  | Device reboots within 10 s; `rst:0xc` on next boot. |
| LOG-001    | Serial boot log          | Boot fresh, observe `idf.py monitor`.                                                                  | Tag/timestamp/version visible. |
| LOG-003    | Boot logs firmware ver   | Grep for `firmware_version=` in monitor output.                                                        | Version line present. |
| LOG-020    | WiFi events logged       | Boot, disable AP, re-enable AP.                                                                        | `connected`, `disconnected`, `got_ip` events all logged. |
| LOG-026    | Error context            | Send a 5 KiB body to `/summary`.                                                                       | Log includes "body too large" with byte count. |
| NFR-1.1    | Warm-boot ≤ 5 s          | Stopwatch reset-to-render; creds in NVS, AP nearby.                                                    | ≤ 5 s. |

### 8.2 Phase 2 Verification

| Test ID    | Feature                  | Procedure                                                                                              | Success Criteria |
|------------|--------------------------|--------------------------------------------------------------------------------------------------------|------------------|
| AP-001     | AP on empty NVS          | Erase NVS, boot.                                                                                       | AP `BURNSCOPE-<XXXX>` visible within 10 s. |
| AP-003     | SSID naming              | Read AP SSID from another device, compare against the device's MAC.                                    | Matches `BURNSCOPE-<last 4 of MAC>`. |
| AP-005     | DHCP on AP               | Connect a phone, check assigned IP.                                                                    | Phone gets IP in `192.168.4.0/24`. |
| CP-001     | Portal served            | Connect to AP, open browser.                                                                           | Portal HTML is the default response. |
| CP-002     | Portal redirect          | From a connected phone, request `http://example.com/foo`.                                              | 302 redirect to `http://192.168.4.1/`. |
| CP-003     | Credential form          | Open portal, submit SSID + password.                                                                   | 200 + reboot. |
| CP-006     | Creds persist            | After CP-003, observe reboot.                                                                          | Device comes up in STA mode, connects to submitted SSID. |
| TC-CP-100  | First-boot E2E           | Full first-boot flow from blank NVS to STA-connected.                                                  | All steps succeed; STA connects. |
| TC-CP-102  | Network scan in portal   | Open WiFi page, inspect listed networks.                                                               | At least the test SSID appears with RSSI. |
| NVS-001    | Config stored            | Provision, read NVS via `nvs_get_str`.                                                                 | Values match what was submitted. |
| NVS-002    | Persist across boots     | Provision, power-cycle.                                                                                | Device reconnects without re-provisioning. |
| NVS-010    | Creds encrypted          | Inspect NVS dump (`esptool read_flash 0x9000 0x6000 …`).                                              | Password not in plain ASCII. (Requires NVS encryption build flag — implementation note in § 10.) |
| NVS-012    | No creds in logs         | Boot, monitor every log line.                                                                          | The submitted password string does not appear in serial or in `GET /health`. |
| TC-NVS-100 | Reboot persistence       | Configure → reboot → power-cycle.                                                                      | Same values throughout. |
| TC-NVS-102 | Factory reset by button  | Long-press boot button ≥ 5 s.                                                                          | NVS erased, AP mode active. |
| TC-NVS-103 | Factory reset by HTTP    | `curl -X POST http://burnscope.local/factory-reset`.                                                   | 202 returned, NVS erased, AP mode active. |
| TC-SUM-100 | Happy-path push          | POST a valid Claude `AgentSnapshot`.                                                                   | 204, UI shows both rows with correct percentages within 200 ms. |
| TC-SUM-101 | Multi-agent push         | POST a Claude snapshot, then a Codex snapshot.                                                         | Both stored separately; UI cycles between them per FR-4.10. |
| TC-SUM-102 | Verbatim labels          | POST a Codex snapshot with `type=primary`/`secondary`.                                                 | Tags render exactly as `primary` / `secondary`. |
| TC-SUM-103 | Bad JSON rejected        | POST malformed JSON.                                                                                   | 400 returned, no change to stored snapshot. |
| TC-SUM-104 | Out-of-range `used_pct`  | POST `used_pct=1.5`.                                                                                   | 400. |
| TC-SUM-105 | Oversized body           | POST 20 KiB body.                                                                                      | 413. |
| TC-HEALTH-100 | Health shape          | `GET /health`.                                                                                         | JSON with required fields; `seconds_since_last_push` increments between pushes. |
| TC-UI-100  | UI matches spec          | Visual inspection against § 6.1.6 sketch.                                                              | Black bg, monospaced text, two rounded rows, header layout correct. |
| TC-UI-101  | Countdown ticks          | Push a snapshot with `resets_at = now()+3600`. Watch.                                                  | Countdown decrements roughly 1/s. |

### 8.3 Phase 3 / Acceptance Tests

| Test ID    | Feature                  | Procedure                                                                                              | Success Criteria |
|------------|--------------------------|--------------------------------------------------------------------------------------------------------|------------------|
| EC-100     | Network disconnect       | (From `wifi-test-spec`) push, disconnect AP, wait 30 s, restore.                                       | Device reconnects, next push paints correctly. |
| EC-101     | WiFi loss during ops     | (From `wifi-test-spec`) disable WiFi, observe locally-driven countdown.                                | Render thread keeps ticking; no reboot. |
| EC-110     | Signal degradation       | (From `wifi-test-spec`) walk away from AP.                                                             | Graceful, no crash. |
| EC-115     | DHCP lease expiry        | (From `wifi-test-spec`) set 60 s lease, wait.                                                          | Renewal succeeds; service still discoverable. |
| EC-CP-200  | Password change on AP    | (From `captive-portal-test-spec`) change AP password upstream.                                         | After N retries, firmware falls back to AP mode (FR-1.6). |
| EC-NVS-200 | NVS corruption           | (From `nvs-test-spec`) corrupt NVS via esptool.                                                        | Boot falls back to AP mode, no crash. |
| EC-NVS-202 | Power loss during write  | Cut power mid-provision.                                                                               | Either old or new value on next boot; never garbage. |
| EC-WDT-200 | WDT stable under flap    | Flap WiFi for 5 min.                                                                                   | No false watchdog resets. |
| AT-1       | 24 h soak                | Continuous pushes every 30 s with periodic WiFi flapping.                                              | No reboots; free heap drift < 5 %. |
| AT-2       | Display swap              | Build with `mock_display` registered instead of the ST7789.                                            | Unit tests on host pass; rendering logic exercised without panel. (FR-5.3) |

### 8.4 Live Verification Log

Snapshot of what has been exercised on real hardware. Update on each
bring-up. Use ✅ for verified, ⏳ for not yet run, ❌ for regressed.

**Hardware:** CYD cyd2usb, STA MAC `d4:e9:f4:b2:f6:4c`, SoftAP MAC ends
`f6:4d` → AP SSID `BURNSCOPE-F64D`, mDNS host `burnscope-f64c.local`.

**Phase 2 first bring-up — 2026-05-18:**

| Test          | Status | Notes |
|---------------|:------:|-------|
| AP-001        | ✅     | AP visible within ~1 s of cold boot. |
| AP-003        | ✅     | SSID `BURNSCOPE-F64D` matches last 4 hex of SoftAP MAC. |
| AP-005        | ✅     | Phone associated; portal flow completed (implies DHCP). |
| CP-001        | ✅     | Portal HTML served on phone connect. |
| CP-002        | ⏳     | Redirect handler registered; not directly exercised. |
| CP-003        | ✅     | Form submission accepted creds. |
| CP-006        | ✅     | After reboot the device joined STA without re-provisioning. |
| TC-CP-100     | ✅     | Full first-boot path: blank NVS → portal → STA. |
| TC-CP-102     | ⏳     | `/scan.json` endpoint present; UI listing not visually confirmed. |
| NVS-001       | ✅     | Creds persisted in `burnscope_wifi` namespace. |
| NVS-002       | ✅     | Survived a power-cycle (RTS reset). |
| NVS-010       | ⏸     | NVS encryption deferred — see §10.4. |
| NVS-012       | ✅     | Password absent from filtered serial log; only SSID logged. |
| TC-NVS-100    | ✅     | Equivalent to NVS-001 + NVS-002. |
| TC-NVS-102    | ⏳     | BOOT-button long-press path not yet exercised. |
| TC-NVS-103    | ⏳     | `POST /factory-reset` path not yet exercised. |
| TC-SUM-100    | ✅     | Smoke script — Claude push 204, panel repaints. |
| TC-SUM-101    | ✅     | Codex push 204; `/health` returns both agents; UI cycles. |
| TC-SUM-102    | ✅     | Codex labels (`primary`, `secondary`) render verbatim. |
| TC-SUM-103    | ✅     | Bad JSON → 400. |
| TC-SUM-104    | ✅     | `used_pct=1.5` → 400. |
| TC-SUM-105    | ✅     | 20 KiB body → 413. |
| TC-HEALTH-100 | ✅     | Shape OK, `agents.*.seconds_since_last_push` increments. |
| TC-UI-100     | ✅     | Layout matches §6.1.6 sketch after a font/contrast polish pass. |
| TC-UI-101     | ✅     | Countdown ticks once per second (after fixing the `s % 60` bug, see below). |
| WIFI-001/003  | ✅     | STA join + auto-reconnect (re-validated during Phase 2). |
| TC-MDNS-100   | ✅     | `burnscope-f64c.local` resolves; smoke script uses it. |
| TC-NTP-100    | ⏳     | Implicit — countdowns now look sensible — but not explicitly timed. |
| TC-WDT-100    | ⏳     | Hang-injection build not re-run for Phase 2. |

**Bugs found and fixed during this bring-up:**

1. **IDF auto-restored a stale `wifi_config_t`.** ESP-IDF's WiFi
   subsystem persists its own copy of `wifi_config_t` in the
   `nvs.net80211` namespace by default. On the first Phase-2 boot the
   STA radio auto-associated with the SSID from the previous Phase-1
   build even though our `burnscope_wifi` namespace was empty. This
   also violated FR-6.1 (our NVS is the *only* persistent store of
   credentials). Fix: call `esp_wifi_set_storage(WIFI_STORAGE_RAM)`
   right after `esp_wifi_init`. Captured at `firmware/main/wifi.c`.

2. **`WIFI_EVENT_STA_START` fired while in AP mode.** APSTA brings up
   both interfaces; without a guard the STA handler emitted
   `WIFI_STATE_CONNECTING` (overwriting the captive-portal splash) and
   tried `esp_wifi_connect()` against an empty SSID. Guarded with
   `s_ap_mode` in the same handler that already protected
   `STA_DISCONNECTED`.

3. **Countdown showed `…58m3515s`.** `format_countdown` printed the
   minutes from `s / 60` but the seconds field used the raw
   post-hour-modulo `s` instead of `s % 60`. One-character fix in
   `firmware/main/displays/cyd2usb_st7789/ui.c`.

4. **Splash text overflowed the panel.** The status label was using
   `lv_obj_center` with no width cap; the literal
   `"Setup mode — connect to BURNSCOPE-XXXX"` extended past the 320 px
   panel. Fix: `LV_LABEL_LONG_WRAP` + explicit 300 px width +
   `LV_TEXT_ALIGN_CENTER`. The placeholder string was also replaced
   with the *real* SSID computed in `provisioning_start`, with an
   added "Open 192.168.4.1" hint for users whose phone OS doesn't
   auto-launch the captive portal.

5. **No Latin serif in stock LVGL 9.5.** A-3 in this FSD assumed
   Roboto Mono. We instead baked Apple **NewYork** at 22 px via
   `lv_font_conv` from `/System/Library/Fonts/NewYork.ttf` (ASCII
   printable range only, ~63 KB). Source lives at
   `firmware/main/fonts/lv_font_newyork_22.c`. A-3 should be considered
   superseded by this concrete choice on the cyd2usb profile.

**Deviations from the FSD recorded during Phase 2 build-out:**

- **cJSON is not in ESP-IDF v6.** The schema is small and regular, so
  `POST /summary` parses inline (~150 lines, no allocations beyond the
  request body) rather than pulling in a third-party managed
  component. The wire-format contract is unchanged.

- **Display abstraction is realised at build time, not runtime.** FSD
  FR-5 specified a runtime `display_t` virtual interface. The
  implementation instead bundles the panel driver and the UI layout
  into a Kconfig-selected profile under
  `firmware/main/displays/<name>/`. UI layout is geometry-bound, so
  one driver-plus-layout unit per screen reads more honestly than a
  runtime polymorphism. The `mock_display` profile envisioned by FR-5.3
  is still possible — it would simply be another build-time profile —
  but is deferred to Phase 3 along with the host-side tests.

### 8.5 Traceability Matrix

| Requirement | Priority | Test Case(s)                              | Status  |
|-------------|----------|-------------------------------------------|---------|
| FR-1.1      | Must     | WIFI-001, TC-NVS-100                      | Covered |
| FR-1.2      | Must     | AP-001, TC-CP-100, TC-NVS-101 *(via empty-NVS path)* | Covered |
| FR-1.3      | Must     | AP-003                                    | Covered |
| FR-1.4      | Must     | CP-003, TC-CP-102                         | Covered |
| FR-1.5      | Must     | CP-006, TC-CP-100                         | Covered |
| FR-1.6      | Should   | EC-CP-200                                 | Covered |
| FR-1.7      | Should   | TC-NVS-102, TC-NVS-103                    | Covered |
| FR-2.1      | Must     | TC-MDNS-100                               | Covered |
| FR-2.2      | Should   | TC-MDNS-100                               | Covered |
| FR-2.3      | Must     | TC-NTP-100                                | Covered |
| FR-2.4      | Should   | WIFI-003, EC-100                          | Covered |
| FR-3.1      | Must     | TC-HTTP-100, TC-SUM-100                   | Covered |
| FR-3.2      | Must     | TC-SUM-103, TC-SUM-104                    | Covered |
| FR-3.3      | Must     | TC-SUM-101                                | Covered |
| FR-3.4      | Should   | TC-HEALTH-100                             | Covered |
| FR-3.5      | May      | TC-NVS-103                                | Covered |
| FR-4.1      | Must     | TC-UI-100                                 | Covered |
| FR-4.2      | Must     | TC-UI-100                                 | Covered |
| FR-4.3      | Must     | TC-UI-100                                 | Covered |
| FR-4.4      | Must     | TC-UI-100                                 | Covered |
| FR-4.5      | Must     | TC-SUM-100, TC-SUM-102                    | Covered |
| FR-4.6      | Must     | TC-SUM-100 (latency component)            | Covered |
| FR-4.7      | Should   | TC-UI-100 (visual)                        | Covered |
| FR-4.8      | Should   | TC-UI-101                                 | Covered |
| FR-4.9      | Should   | AT-1 (stale section), EC-101              | Covered |
| FR-4.10     | May      | TC-SUM-101                                | Covered |
| FR-5.1      | Must     | AT-2                                      | Covered |
| FR-5.2      | Must     | Build inspection during AT-2              | Covered |
| FR-5.3      | Should   | AT-2                                      | Covered |
| FR-6.1      | Must     | NVS-001, TC-NVS-100                       | Covered |
| FR-6.2      | Must     | NVS-012, EC-NVS-203 *(adapted)*           | Covered |
| NFR-1.1     | Must     | NFR-1.1 row in § 8.1                      | Covered |
| NFR-1.2     | Should   | AP-001 (timing variant)                   | Covered |
| NFR-2.1     | Must     | TC-SUM-100                                | Covered |
| NFR-2.2     | Should   | TC-SUM-105                                | Covered |
| NFR-3.1     | Must     | AT-1                                      | Covered |
| NFR-3.2     | Must     | TC-WDT-100, EC-WDT-200                    | Covered |
| NFR-4.1     | Should   | LOG-001, LOG-020                          | Covered |
| NFR-5.1     | Should   | TC-NTP-100, TC-UI-101                     | Covered |

---

## 9. Troubleshooting Guide

| Symptom                                   | Likely Cause                                | Diagnostic Steps                                                              | Corrective Action |
|-------------------------------------------|---------------------------------------------|-------------------------------------------------------------------------------|-------------------|
| AP not visible after first power-on       | NVS contains old creds; device is in STA mode | Erase NVS via esptool or hold the boot button ≥ 5 s.                          | Re-provision via captive portal. |
| AP visible but portal does not open       | Phone's captive-portal probe blocked        | Manually navigate to `http://192.168.4.1/`.                                   | Filed against FR-1.4 if it recurs. |
| Daemon can't find the device              | mDNS blocked on the LAN                     | Try `ping burnscope-<XXXX>.local`; if that fails, look up IP from router.    | Use daemon's `--esp32-host` override. |
| `POST /summary` returns 400               | JSON shape mismatch                         | Check `Content-Type` and validate against `docs/wire-format.md`.              | Fix daemon payload. |
| UI shows correct % but countdown wrong    | Wall clock not synced                       | `GET /health`, check whether device has had ≥ 30 s of network since boot.    | Wait for SNTP, or check firewall on UDP 123. |
| Screen pixels glitch / tear               | SPI clock too high or wrong pin             | Confirm `LCD_PIXEL_CLOCK_HZ = 20 MHz` in `main.c`.                            | Reduce clock; ensure cyd2usb pinout. |
| Device reboots ~1×/minute                 | Watchdog firing                             | Check `idf.py monitor` for `Task watchdog got triggered`; identify task.     | Add heartbeat in offending task; revisit timeouts. |
| Boot reason `rst:0xc` after every reboot  | Watchdog instability                        | Inspect last health-check logs; compare against EC-WDT-200.                  | File bug with stack trace. |

---

## 10. Appendix

### 10.1 Constants & defaults

| Constant                  | Default                   | Source |
|---------------------------|---------------------------|--------|
| SPI pclk                  | 20 MHz                    | `firmware/main/main.c` (C-2) |
| Panel resolution          | 320×240 landscape         | `firmware/main/main.c` |
| Pixel format              | RGB565, swap-bytes, BGR   | `firmware/main/main.c` |
| LVGL color depth          | 16 bpp                    | `sdkconfig.defaults` |
| LVGL font (header)        | Montserrat 24            | `sdkconfig.defaults` |
| LVGL font (body)          | Monospace; Roboto Mono (assumed, per A-3) | This FSD |
| WiFi STA reconnect backoff| 1 s → 30 s exponential    | This FSD |
| STA auth-failure threshold| 5                         | This FSD |
| Captive-portal AP SSID    | `BURNSCOPE-<XXXX>`        | FR-1.3 |
| Captive-portal AP IP      | `192.168.4.1`             | IDF default |
| mDNS service              | `_burnscope._tcp.local`   | `docs/description.md` |
| HTTP port                 | 80                        | This FSD |
| Max `POST /summary` body  | 16 KiB                    | NFR-2.2 |
| SNTP pool                 | `pool.ntp.org`            | This FSD |
| SNTP resync interval      | 6 h                       | This FSD |
| Watchdog (TWDT) timeout   | 10 s                      | NFR-3.2 |
| Snapshot keepalive (info) | ~30 s                     | `docs/wire-format.md` |
| Stale threshold (UI)      | 2 × keepalive ≈ 60 s      | FR-4.9 |

### 10.2 Pinout (CYD cyd2usb variant)

| Function       | GPIO |
|----------------|------|
| Display SCLK   | 14   |
| Display MOSI   | 13   |
| Display MISO   | 12   |
| Display DC     | 2    |
| Display CS     | 15   |
| Display RST    | tied to system EN |
| Backlight      | 21   |

Source: `firmware/main/main.c`.

### 10.3 Example wire payload

```json
{
  "agent": "claude",
  "captured_at": 1779050146,
  "sessions": [
    { "type": "5h", "used_pct": 0.03, "resets_at": 1779066600 },
    { "type": "7d", "used_pct": 0.09, "resets_at": 1779156000 }
  ]
}
```

Source: `docs/examples/summary-push.json`.

### 10.4 NVS encryption note (implementation)

NVS-010 in `nvs-test-spec.md` requires "encrypted in NVS". The CYD has
no secure element, so true NVS encryption needs the ESP32 flash-encryption
+ `nvs_flash_secure_*` API path. MVP may ship with this disabled and
treat the requirement as "credentials are stored in NVS, not echoed to
logs, and not exposed via any HTTP endpoint" (covered by FR-6.2 and
NVS-012). Full flash encryption is recommended but not gating MVP.

---

## 11. Related

- `[[burnscope/docs/description.md]]` — project-level scope and architecture.
- `[[burnscope/docs/wire-format.md]]` — daemon ↔ firmware JSON contract; source of truth for § 6.3.
- `[[burnscope/docs/examples/summary-push.json]]` — canonical example payload.
- `[[burnscope/CLAUDE.md]]` — repository-wide developer notes.
