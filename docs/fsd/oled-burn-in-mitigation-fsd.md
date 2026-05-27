# BurnScope OLED Burn-in Mitigation — Functional Specification Document (FSD)

> **Scope:** A bundle of five mitigations that protect the
> `amoled_sh8601` display profile (Waveshare ESP32-S3-Touch-AMOLED-1.43,
> 466×466 round AMOLED) from cumulative pixel wear, plus the paired
> client-side change in the Codex daemon that makes idle screen-off
> actually achievable. Mitigations are: orientation-driven layout
> rotation, idle dimming and screen-off with multi-source wake, default
> brightness cap, palette-acceptance check, and 1-px feathered edges.
>
> **Inputs to this FSD:** `docs/oled_burnin_mitigation.md` § "AMOLED
> build profile — concrete decisions" (the source of truth for design
> defaults), `docs/fsd/firmware-fsd.md` (existing firmware contract;
> the `POST /summary` handler is the upstream of the `EV_PUSH` event),
> `docs/wire-format.md` (`AgentSnapshot` shape — basis of the semantic-
> equality check), and the Waveshare reference demo at
> `ESP32-S3-AMOLED-1.43-Demo/ESP-IDF/03_I2C_QMI8658` for the QMI8658
> IMU bring-up.
>
> **Out of scope:** Other displays (CYD), pixel-shift / orbit
> (§1.1 of the catalog — separate FSD when scheduled), Logo Luminance
> Adjustment (§3.3), pixel-refresh / demura cycles (§5.x), ambient-
> light sensing.

---

## 1. System Overview

### 1.1 Purpose

The AMOLED build profile is an always-on UI on an emissive display. Every
hour the same gauge arcs, text labels, and identifier strings hit the
same subpixels — exactly the wear pattern that produces visible
ghosting ("burn-in") within months on consumer OLEDs. This FSD
specifies the firmware and client-side measures that keep the visible
UI within a wear budget the panel can survive for the design life of
the device.

### 1.2 Problem Statement

Five concrete problems, each addressed by one of the five mitigations:

1. **Static layout** — gauges and identifier text sit in fixed pixels
   for hours. Solved by **A1** orientation rotation (when the device
   moves) and **A2** screen-off / dim (when it doesn't).
2. **Long lit periods with no user looking** — full brightness 24/7
   even when the user is asleep or at lunch. Solved by **A2** idle
   dim + screen-off.
3. **Codex daemon's long-lived `app-server` cache is stale and
   un-notified** — it doesn't observe rate-limit changes that
   originate in other `codex` CLI processes on the same machine, so
   left to the `rateLimits/updated` notification stream alone it
   would push exactly once at bootstrap and never again. Solved by
   client-side **active poll + anchor + dedupe**: read every minute,
   anchor `resets_at` to the last-pushed value on each session whose
   `used_pct` is unchanged (codex's `resetsAt` drifts ~60 s per 60 s
   of wall-clock at low usage, so without anchoring the dedupe key
   would change every poll), and push only when the anchored
   snapshot differs from the last successfully pushed one. The
   firmware compensates for the stale stored `resets_at` by
   synthesising `now + window_duration_mins * 60` at render time
   when the session is `rolling=true` and `used_pct ≤ 0.01`. This
   keeps the firmware fresh *and* preserves the silent-when-idle
   property A2 needs.
4. **100 % brightness wears the panel super-linearly** — solved by
   **A3** default-brightness cap at 70 %.
5. **Pure white and saturated blue ghost worst** — solved by **A4**
   palette acceptance check (no `#FFFFFF`, no `#0000FF`-class
   blues).
6. **Sharp 1-px edges ghost as crisp wear lines** — solved by **A5**
   1-px anti-aliasing / feathering on every static boundary.

### 1.3 Users / Stakeholders

- **End user (developer):** never interacts with the mitigations
  directly. Notices: the screen dims when ignored, wakes when picked
  up, rotates with the device, and looks visually softer.
- **Codex daemon (`codex_daemon.py`):** owns active polling +
  anchoring + dedupe. Polls `account/rateLimits/read` once a
  minute, anchors `resets_at` to the last-pushed value when
  `used_pct` is unchanged (to defeat codex's wall-clock-driven
  `resetsAt` drift), and produces a push only when the anchored
  result differs from the last successfully pushed snapshot, so the
  firmware's idle state machine can sleep when nothing is changing.
- **Firmware integrator** swapping panels: reuses the pure-logic
  idle state machine on a new display profile by writing a new
  adapter; SM is unchanged.
- **UI designer:** must keep colour tokens and primitive rendering
  within the A4 / A5 constraints.

### 1.4 Goals & Non-Goals

**Goals:**

- The idle state machine is implemented as a pure-logic module with
  zero ESP-IDF dependencies, host-testable, reusable across display
  profiles (FR-2.x).
- Codex daemon polls `account/rateLimits/read` every minute,
  anchors `resets_at` to the last-pushed value when `used_pct` is
  unchanged, and pushes only when the anchored `AgentSnapshot`
  differs from the last successfully pushed one (FR-4.x). The
  wire format carries `rolling: bool` and `window_duration_mins:
  int` per session so the firmware can synthesise its countdown
  during idle without waking on a refresh push.
- The AMOLED profile dims at 5 min idle and turns off at 30 min idle
  (defaults; both Kconfig-configurable). Direct user interactions
  (accelerometer motion, touch, button) wake the panel immediately to
  ACTIVE. A qualifying `POST /summary` is a *soft* wake — from OFF or
  DIMMED it lifts the panel only to DIMMED, extending the dim-to-off
  countdown by `(off_after_us − dim_after_us)`; from ACTIVE it
  refreshes the idle timer without changing state. The rationale is
  that pushes reflect upstream activity, not direct user attention,
  so they should not commit the panel to full brightness on their own
  (FR-2.3, FR-3.x).
- The UI rotates in 90° steps to follow the device's orientation
  (FR-1.x).
- No static UI element renders as `#FFFFFF`-grade white or as
  `#0000FF`-grade saturated blue (FR-5.x).
- All static UI boundaries (text, gauges, dividers) render with
  ≥ 1-px anti-aliasing (FR-6.x).

**Non-goals:**

- Pixel-shift / orbit. The orientation rotation is a coarse cousin
  and is sufficient to ship Phase 1. Full pixel-shift is a separate
  spec.
- Battery-driven panel sleep (panel sleep is via the SH8601 sleep
  command, not Wi-Fi/CPU sleep — Wi-Fi must stay up to receive
  pushes).
- Per-region wear tracking, demura, panel-refresh commands.
- Ambient-light adaptive brightness (no LDR on the board).
- Replacing the existing `cyd2usb_st7789` profile's behaviour — the
  pure idle SM is *available* to it but adopting it is out of scope.
- Migrating Claude statusline to a dedupe path — its natural cadence
  already correlates with user activity; revisit only if redundant
  intra-session pushes become a problem.

### 1.5 High-Level System Flow

```
                          ┌─────────── adapter (amoled_sh8601) ───────────┐
                          │                                                │
QMI8658 accel @ 21 Hz ─►──┤ orient. detect ──► UI rotation (LVGL)         │
                          │                                                │
                          │ Δg > threshold ──► EV_MOTION ─┐                │
Touch IRQ        ─►───────┤                   EV_TOUCH ─┐ │                │
Button IRQ       ─►───────┤                   EV_BUTTON│ │                │
HTTP POST /sum   ─►───────┤                     EV_PUSH│ │                │
1 Hz esp_timer   ─►───────┤                     EV_TIME│ │                │
                          │                            ▼ ▼                │
                          │            ┌──── burn_idle_step() ────┐       │
                          │            │  pure logic (burn_idle.c)│       │
                          │            └──── output struct ───────┘       │
                          │                            │                  │
                          │                            ▼                  │
                          │            sh8601_brightness() / sleep        │
                          └────────────────────────────────────────────────┘

                          ┌──────────────── client side (laptop) ─────────┐
1-min poll ─►──── account/rateLimits/read ─►── AgentSnapshot ─────────► │
                          │  (notifications also handled if they fire,    │
                          │   but cross-process emission is unreliable —  │
                          │   see § 5.2 A-4. Poll is the authoritative    │
                          │   trigger.)                                   │
                          │                                                │
                          │  anchored = _anchor_resets_at(new, _last_p.)  │
                          │      (rewrites resets_at to last-pushed value │
                          │       per session when used_pct is unchanged) │
                          │  semantically_equal(anchored, _last_pushed)?  │
                          │      yes ──► drop (log debug)                  │
                          │      no  ──► enqueue → push → update last      │
                          └────────────────────────────────────────────────┘
```

---

## 2. System Architecture

### 2.1 Logical Architecture

Two new modules and one new helper, distributed across firmware and
client:

| Component                      | Where                                         | Role |
|--------------------------------|-----------------------------------------------|------|
| **Idle state machine**         | `firmware/main/burn_protection/burn_idle.{h,c}` | Pure-logic SM. Inputs: events + monotonic time. Outputs: `{state, brightness_pct, panel_on, changed}`. Zero ESP-IDF deps. |
| **AMOLED idle adapter**        | `firmware/main/displays/amoled_sh8601/burn_idle_adapter.c` | Wires IMU sampling, touch IRQ, button IRQ, `POST /summary` callback, and a 1 Hz `esp_timer` into the SM; applies SM outputs to the SH8601 driver. |
| **Orientation detector** *(Phase 3 — landed; IMU-ROT-* hardware tests pending)* | `firmware/main/displays/amoled_sh8601/orientation.c` | Reads accelerometer gravity vector, picks quadrant with hysteresis + debounce, drives `lv_display_set_rotation`. |
| **Codex active poll + dedupe** | `client/src/burnscope_client/codex_daemon.py` (modified) + `client/src/burnscope_client/schema.py` (helper) | `AgentSnapshot.semantically_equal(other)` plus a new `_poll_loop` that calls `account/rateLimits/read` every `POLL_INTERVAL_S` and only enqueues when the result differs from `_last_pushed_snapshot`. |
| **Palette validator** *(Phase 4 — not yet shipped)* | `firmware/main/displays/amoled_sh8601/palette_check.c` (or CMake-time script) | Static check that all colour tokens used by the AMOLED UI satisfy A4. |

### 2.2 Hardware / Platform Architecture

| Element            | Detail                                                                                        |
|--------------------|-----------------------------------------------------------------------------------------------|
| MCU                | ESP32-S3 (Waveshare ESP32-S3-Touch-AMOLED-1.43 module)                                        |
| Panel              | 466×466 round AMOLED, SH8601 / CO5300 controller, QSPI                                        |
| IMU                | QMI8658 6-axis (only the accelerometer is used for this feature)                              |
| Touch              | FT3168 capacitive touch over I²C (no INT line routed on this board); polled via an LVGL pointer input device. |
| Button             | Onboard pushbutton (board-specific GPIO; reuse the BOOT-button long-press wiring conceptually) |
| Bus                | I²C for IMU and touch; QSPI for panel                                                         |
| Brightness control | SH8601 brightness register via `esp_lcd_panel_io_tx_param`                                    |

### 2.3 Software Architecture

```
firmware/main/
├── burn_protection/
│   ├── burn_idle.h            # pure SM API
│   ├── burn_idle.c            # pure logic, <stdint.h>/<stdbool.h>/<stddef.h>/<assert.h> only
│   ├── Kconfig                # tunables: thresholds, default brightness
│   └── test/
│       ├── burn_idle_test.c   # host unit tests (custom ~60-line harness)
│       └── CMakeLists.txt     # host-only target
└── displays/amoled_sh8601/
    ├── driver.c               # existing — SH8601 bring-up
    ├── ui.c                   # existing — LVGL layout (now also hosts the touch indev callback)
    ├── touch.c                # SHIPPED (Phase 2 PR-1) — polled FT3168 reader for the LVGL indev
    ├── qmi8658.c              # SHIPPED (Phase 2 PR-2) — accel-only QMI8658 driver (motion wake)
    ├── burn_idle_adapter.c    # SHIPPED (Phase 2 PR-1 + PR-2) — events + outputs ↔ hardware
    ├── orientation.c          # SHIPPED (Phase 3) — accel → quadrant → lv_display_set_rotation (IMU-ROT-* pending hardware verification)
    └── palette_check.c        # PLANNED (Phase 4) — token validator (or build-time .py)

client/src/burnscope_client/
├── schema.py                  # +AgentSnapshot.semantically_equal()
└── codex_daemon.py            # +_last_pushed_snapshot, dedupe in _push_one path
```

**SM purity contract** (FR-2.1): `burn_idle.c` must compile cleanly
with a host C compiler. The contract enforces:

- No `#include "esp_*"`, `freertos/*`, `driver/*`, `lv_*`, or
  `esp_lcd_*`.
- All time arguments are `int64_t` microseconds — the SM has no
  opinion on the clock source.
- Output state changes are reported, not applied; the adapter
  decides whether and how to act on `output.brightness_pct` and
  `output.panel_on`.

---

## 3. Implementation Phases

### 3.1 Phase 1 — Idle State Machine (pure module)

**Scope.** `burn_protection/burn_idle.{h,c}` and host-runnable unit
tests. No firmware integration. No hardware.

**Deliverables.**

- `burn_idle.h` exposing `burn_idle_t`, `burn_idle_event_t`,
  `burn_idle_output_t`, `burn_idle_config_t`, and
  `burn_idle_step(sm, ev, now_us)`.
- `burn_idle.c` implementing the state machine.
- Host CMake target running the test suite outside ESP-IDF.

**Exit criteria.** All SM-* tests in § 8.1 pass on host. Coverage of:
each event type, each state transition, brightness-table lookup,
flapping/debounce-of-tick, config edge cases.

**Dependencies.** None.

### 3.2 Phase 2 — AMOLED Adapter (firmware-side)

**Scope.** The end-to-end idle behaviour on the AMOLED hardware.
The previously-paired client-side dedupe work shipped independently
ahead of this phase (see "Codex poll + dedupe (shipped)" below);
the adapter can now rely on `POST /summary` arrivals being
genuinely meaningful when they do happen.

**Deliverables.**

- `burn_idle_adapter.c`: IMU sampler (with motion thresholding),
  touch wake (driven by the LVGL touch indev's read callback in
  `ui.c` on a rising-edge press), button IRQ binding, 1 Hz
  `esp_timer` for `EV_TIME`, snapshot listener hooked to `EV_PUSH`,
  brightness + panel sleep/wake actions on SM output. Brightness
  changes go through a fade engine — a second `esp_timer` runs at
  ~30 Hz only while a ramp is in flight, interpolating between the
  current 0x51 register value and the SM's target so the panel
  visibly ramps rather than snaps.

**Exit criteria.** Phase 2 hardware tests in § 8.2 pass: manual
dim/off timing, all four wake sources, push-with-no-change does
not wake (relies on the client-side dedupe already shipped),
push-with-change soft-wakes the panel to DIMMED (FR-2.3).

**Dependencies.** Phase 1. Implicit dependency on the shipped
Codex poll + dedupe to satisfy the WAKE-005 ("non-qualifying push
does not wake") test.

#### Phase 2a (already shipped) — Codex poll + dedupe

Landed as a `fix/` branch ahead of the firmware adapter because
the daemon's notification path was found to be broken in practice
(see § 5.2 A-4). Delivers:

- `schema.py`: `AgentSnapshot.semantically_equal(other)` — no
  tolerance kwarg.
- `codex_daemon.py`: `POLL_INTERVAL_S = 60`,
  `_last_pushed_snapshot`, `_poll_loop` coroutine gathered
  alongside `_pusher_loop` and `_health_loop`,
  `_last_pushed_snapshot` advanced only on successful push.
- `tests/test_schema.py` + extensions to `tests/test_codex_daemon.py`.

### 3.3 Phase 3 — Orientation Rotation

**Scope.** Read accelerometer; rotate the LVGL display in 90°
quadrants with hysteresis + debounce.

**Deliverables.**

- `orientation.c`: low-power accel sampling, dominant-axis selector
  with ±0.2 g hysteresis band, 500 ms debounce, calls into LVGL's
  rotation API and into the framebuffer flush pipeline (verify
  `lv_display_set_rotation` is sufficient for the SH8601 driver — fall
  back to manual rotation if not).
- New Kconfig option to disable rotation for diagnostic builds.

**Exit criteria.** All IMU-ROT-* tests in § 8.3 pass: each quadrant
holds, transitions at the expected thresholds, no flapping at 45°,
rotation is debounced.

**Dependencies.** None — independent of Phases 1 and 2.

### 3.4 Phase 4 — UI Constraints (palette + soft edges)

**Scope.** Static checks and rendering changes that pull every UI
element into the A4 / A5 envelopes.

**Deliverables.**

- `palette_check.c` (or `tools/palette_check.py` run from CMake) that
  walks the AMOLED UI's colour tokens and fails the build on
  violations.
- LVGL config: `LV_DRAW_SW_COMPLEX = 1`, anti-aliased fonts enabled
  for the AMOLED profile.
- Any hand-drawn primitive in `ui.c` updated to feather its edge at
  ~50 % intensity.

**Exit criteria.** Build fails when a forbidden colour token is
introduced. Visual review of rendered text and arcs confirms ≥ 1-px
feathering (verified by a screen-grab pixel inspection, § 8.4).

**Dependencies.** None.

### 3.5 Phase 5 — Acceptance

**Scope.** Long-duration soak and end-to-end verification.

**Deliverables.** Updated Live Verification Log (§ 8.5).

**Exit criteria.** 24 h soak passes (AT-1): brightness states cycle
correctly across motion / push / idle phases; no spurious wakes; no
panel state leaks; client-side dedupe maintains a steady-state
no-push rate when usage is unchanged.

**Dependencies.** Phases 1–4 complete.

---

## 4. Functional Requirements

### 4.1 Functional Requirements (FR)

**FR-1 Orientation Rotation**

- **FR-1.1** [Must]: The AMOLED profile shall read the QMI8658
  accelerometer at a low-power output data rate sufficient to detect
  manual reorientations (default: `Qmi8658AccOdr_LowPower_21Hz`).
- **FR-1.2** [Must]: The profile shall select the UI orientation
  (0°, 90°, 180°, 270°) by identifying which of ±X / ±Y dominates
  the measured gravity vector.
- **FR-1.3** [Must]: A new dominant axis shall not be committed
  unless its gravity component exceeds the current dominant axis's
  by at least the hysteresis band (default: 0.2 g).
- **FR-1.4** [Must]: A new orientation, once selected, shall only
  take effect after remaining stable for the debounce window
  (default: 500 ms).
- **FR-1.5** [Should]: Orientation detection shall use only the
  accelerometer; the gyroscope shall not be read for this purpose
  (drift, no benefit).
- **FR-1.6** [May]: A Kconfig option shall allow disabling rotation
  for diagnostic builds while leaving the IMU adapter live for
  motion detection (FR-2.7).

**FR-2 Idle State Machine (pure module)**

- **FR-2.1** [Must]: The idle state machine shall be implemented as
  a pure-logic C module (`burn_idle.{h,c}`) with no ESP-IDF, LVGL,
  FreeRTOS, or panel-driver dependencies; it shall compile cleanly
  with a host C compiler.
- **FR-2.2** [Must]: The SM shall expose exactly three states:
  `BURN_IDLE_ACTIVE`, `BURN_IDLE_DIMMED`, `BURN_IDLE_OFF`.
- **FR-2.3** [Must]: The SM shall accept events
  `EV_MOTION`, `EV_TOUCH`, `EV_BUTTON`, `EV_PUSH`, and `EV_TIME`. The
  direct-interaction wake events (`EV_MOTION`, `EV_TOUCH`,
  `EV_BUTTON`) shall reset `last_activity_us` to `now_us` and
  transition the SM to `BURN_IDLE_ACTIVE`. `EV_PUSH` is a *soft
  wake*: from `BURN_IDLE_OFF` or `BURN_IDLE_DIMMED` it shall set
  `last_activity_us = now_us − dim_after_us` and transition to
  `BURN_IDLE_DIMMED` (so the SM falls back to `BURN_IDLE_OFF` after
  `(off_after_us − dim_after_us)` more silence — sustained pushes
  extend the DIMMED phase but never grant a fresh full
  `off_after_us` window); from `BURN_IDLE_ACTIVE` it shall reset
  `last_activity_us` to `now_us` without changing state. Rationale:
  an arriving `POST /summary` reflects upstream activity
  (semantically meaningful per the FR-4 dedupe) but is not a direct
  user interaction, so it must not commit the panel to full
  brightness on its own.
- **FR-2.4** [Must]: On `EV_TIME`, the SM shall transition to
  `BURN_IDLE_DIMMED` when `now - last_activity ≥ dim_after_us`, and
  to `BURN_IDLE_OFF` when `now - last_activity ≥ off_after_us`. The
  Off threshold shall be the larger of the two.
- **FR-2.5** [Must]: The SM's step function shall return a
  `{state, brightness_pct, panel_on, changed}` output struct. The
  `changed` field shall be true iff any field differs from the
  previous step.
- **FR-2.6** [Must]: `brightness_pct` and `panel_on` shall be a
  function only of the current state and the supplied
  `burn_idle_config_t` (state-to-brightness lookup). They shall not
  be hard-coded in the SM.
- **FR-2.7** [Should]: The SM shall expose an init function that
  zeroes timestamps to a defined epoch and sets the initial state
  to `BURN_IDLE_ACTIVE`.

**FR-3 AMOLED Idle Adapter (firmware integration)**

- **FR-3.1** [Must]: The adapter shall poll the accelerometer at the
  rate defined by FR-1.1, compute per-sample delta-magnitude against
  the previous sample, and emit `EV_MOTION` when the magnitude
  exceeds the configured motion threshold (default: 50 mg).
- **FR-3.2** [Must]: The adapter shall expose an entry point
  (`burn_idle_adapter_notify_touch`) that emits `EV_TOUCH`. The LVGL
  touch input-device read callback (registered against the FT3168
  in the AMOLED `ui.c`) calls it on a rising-edge press —
  released → pressed — so a continuously-held finger does not
  re-post. The Waveshare 1.43" board exposes no INT line on the
  FT3168, so polling via the LVGL indev tick is the only available
  mechanism; LVGL's input timer keeps running while the panel is
  asleep, so touch wake remains functional in `BURN_IDLE_OFF`.
- **FR-3.3** [Must]: The adapter shall bind the on-board button to
  a handler that emits `EV_BUTTON` on press.
- **FR-3.4** [Must]: The adapter shall register a snapshot listener
  via `snapshot_store_register_listener` and emit `EV_PUSH` for
  every snapshot put. (The Codex daemon's dedupe — FR-4.x —
  guarantees these are semantically meaningful.)
- **FR-3.5** [Must]: The adapter shall run a 1 Hz `esp_timer` that
  calls `burn_idle_step(sm, EV_TIME, now_us)` once per second.
- **FR-3.6** [Must]: On any `output.changed == true` step, the
  adapter shall drive `brightness_pct` to the SH8601 brightness
  register through a linear fade engine and, on `panel_on`
  transitions, call panel sleep or wake. The fade ramps from the
  panel's current register value to the SM's target over
  `BURNSCOPE_AMOLED_WAKE_FADE_MS` (when the target is brighter
  than the current value or the panel is waking from OFF) or
  `BURNSCOPE_AMOLED_SLEEP_FADE_MS` (when the target is dimmer or
  the panel is powering down). Wake-first / sleep-last ordering
  shall be preserved: `DISPON` runs before the fade-up begins; for
  panel-off transitions `DISPOFF` runs only after the fade-down
  reaches 0. A new fade shall preempt any in-flight fade by
  re-anchoring its start point at the current interpolated value,
  so a wake event during a dim-down reverses direction smoothly.
  Setting either Kconfig duration to 0 shall fall back to the
  instantaneous register write (FR-3.6 legacy behaviour).
- **FR-3.7** [Must]: While in `BURN_IDLE_OFF`, the HTTP server,
  Wi-Fi, mDNS, and snapshot store shall continue to operate; an
  incoming `POST /summary` shall update the framebuffer and emit
  `EV_PUSH`, soft-waking the panel to `BURN_IDLE_DIMMED` per
  FR-2.3.
- **FR-3.8** [Should]: The motion threshold, dim threshold, off
  threshold, default brightness, dimmed brightness, wake fade
  duration, and sleep fade duration shall be exposed as Kconfig
  options under `BurnScope display → AMOLED burn-in`.

**FR-4 Codex Daemon Active Poll + Push Dedupe**

- **FR-4.1** [Must]: `codex_daemon.py` shall maintain a
  `_last_pushed_snapshot` attribute, initialised to `None`.
- **FR-4.2** [Must]: The daemon shall run a `_poll_loop` coroutine
  alongside `_pusher_loop` and `_health_loop`. Every
  `POLL_INTERVAL_S` seconds (default: 60), the loop shall send an
  `account/rateLimits/read` JSON-RPC request to its own
  `codex app-server` subprocess and convert the response into an
  `AgentSnapshot` via the existing `_snapshot_from_rate_limits`
  helper.
- **FR-4.3** [Must]: The freshly-built snapshot shall be compared
  against `_last_pushed_snapshot` using a semantic-equality
  predicate (FR-4.4 / FR-4.5). If the predicate reports equal, the
  snapshot shall not be enqueued; the event shall be logged at
  debug level. If unequal (or if `_last_pushed_snapshot is None`),
  the snapshot shall be enqueued for the existing `_pusher_loop`.
- **FR-4.4** [Must]: The semantic-equality predicate shall be
  insensitive to `captured_at` (which bumps on every read) and
  shall compare `used_pct` byte-exact. No numeric tolerance: the
  Codex wire format types `usedPercent` as `f64` but the
  ChatGPT-plan backend ships integer values, and the daemon's
  `_anchor_resets_at` runs *before* this predicate so the
  separately-drifting `resets_at` field doesn't contaminate the
  comparison.
- **FR-4.5** [Must]: For non-numeric fields (`agent`,
  `sessions[*].type`, and the set of `(type)` keys present), the
  predicate shall also require byte-exact equality. Session order
  shall not be significant (sorted before comparison). A change in
  the set of session types is by definition a state transition.
- **FR-4.6** [Must]: `_last_pushed_snapshot` shall be updated when
  *any* device's `PushResult.ok == True` — i.e. as soon as at least
  one paired device has the snapshot, the dedupe baseline advances
  so a working device is not re-pushed every minute because of a
  flaky peer. A wholesale failure (no device accepted) shall leave
  the previous value untouched so the next poll iteration retries
  the same content. Distinct from `overall_ok`, which reports
  per-agent push health to `burnscope status` and is True only
  when every kept device succeeded.
- **FR-4.7** [Should]: The poll interval shall live in a named
  module-level constant (`POLL_INTERVAL_S`) so tests can patch it
  to a sub-second cadence without changing daemon logic.
- **FR-4.8** [Should]: The existing `_dispatch` handler for
  `account/rateLimits/updated` notifications shall be preserved
  unchanged. Cross-process emission of these notifications is
  unreliable today (see § 5.2 A-4), but the path costs nothing and
  is a free win if a future codex release fixes it.
- **FR-4.9** [May]: The `semantically_equal` predicate lives on
  `AgentSnapshot` and shall not depend on Codex-specific state, so
  a future Claude-statusline dedupe path can reuse it without
  modification.

**FR-5 Palette Acceptance Check**

- **FR-5.1** [Must]: For the AMOLED profile, no colour token used
  by any static UI element shall have its three 8-bit channels all
  ≥ `0xC0` simultaneously (the "no pure white" rule).
- **FR-5.2** [Must]: For the AMOLED profile, no colour token shall
  satisfy both *(a)* the blue channel ≥ `0xB0` *and* *(b)* `R < 0x40`
  *and* `G < 0x40` (the "no saturated blue" rule).
- **FR-5.3** [Should]: The palette rules shall be enforced
  automatically — either by a small C / Python static check run at
  build time, or by a unit-tested validator over an exported
  colour-token table — such that a violating change cannot land
  silently.

**FR-6 Soft Edges**

- **FR-6.1** [Must]: All static text in the AMOLED UI shall render
  with LVGL's anti-aliased font path enabled (no 1-bpp glyphs).
- **FR-6.2** [Must]: `LV_DRAW_SW_COMPLEX` shall be set to `1` in the
  AMOLED profile so that arcs and rounded primitives feather their
  edges instead of producing sharp staircases.
- **FR-6.3** [Should]: Hand-drawn primitives (custom gauges,
  dividers) in `displays/amoled_sh8601/ui.c` shall blend their edge
  pixels at ≈ 50 % intensity rather than rendering with a hard
  on/off boundary.

### 4.2 Non-Functional Requirements (NFR)

- **NFR-1.1** [Must]: `burn_idle_step` shall execute in O(1) time
  with no dynamic allocation. Wall-time on ESP32-S3 at 240 MHz
  shall not exceed 100 µs per call (measured for the slowest event
  branch).
- **NFR-1.2** [Should]: The IMU adapter at 21 Hz sampling shall
  consume < 2 % of one CPU core averaged over a 60 s window in
  Active state.
- **NFR-2.1** [Must]: Wake responsiveness shall be ≤ 200 ms at the
  95th percentile, measured per wake class as *event → first
  visible brightness change* (i.e. the leading edge of the fade
  ramp, not its completion):
  - Direct-interaction wakes (`EV_MOTION`, `EV_TOUCH`,
    `EV_BUTTON`): event → fade toward `active_brightness_pct`
    starts.
  - Soft wakes (`EV_PUSH`): event → fade toward
    `dimmed_brightness_pct` starts if the panel was in
    `BURN_IDLE_OFF` or `BURN_IDLE_DIMMED`; no panel write if the
    panel was in `BURN_IDLE_ACTIVE` (output unchanged per FR-2.3).
  - The fade itself takes `BURNSCOPE_AMOLED_WAKE_FADE_MS`
    (default 300 ms) to reach the target; setting that Kconfig to
    0 collapses fade completion onto the 200 ms responsiveness
    budget.
- **NFR-2.2** [Should]: Brightness transitions (Dim, Off, Wake) are
  ramped by the adapter's fade engine — linear interpolation at
  ~30 Hz between the panel's current 0x51 value and the SM's
  target, driven by a dedicated `esp_timer` that only runs while a
  ramp is in flight. Ramp durations are split by direction so a
  wake feels responsive (`BURNSCOPE_AMOLED_WAKE_FADE_MS`, default
  300 ms) while a dim feels gentle
  (`BURNSCOPE_AMOLED_SLEEP_FADE_MS`, default 1500 ms). The fade
  lives entirely in the adapter; the SM remains pure and emits
  step targets only.
- **NFR-3.1** [Must]: In steady-state operation where upstream
  rate-limit values are unchanged, the daemon shall produce zero
  `POST /summary` pushes (modulo the first-run push at bootstrap).
  This is the load-bearing property that lets the firmware's idle
  SM reach Dimmed and Off.
- **NFR-3.2** [Should]: A single `_poll_loop` iteration (read +
  semantic-equality check) shall complete in ≤ 50 ms on reference
  hardware (Apple Silicon laptop, Python 3.14, codex-cli 0.133+),
  excluding the 60 s sleep between iterations.
- **NFR-4.1** [Must]: `burn_idle.c` shall compile cleanly with a
  host C99 compiler in a target without any ESP-IDF environment;
  the host test suite shall be runnable on macOS and Linux CI.
- **NFR-5.1** [Should]: SM tests shall cover ≥ 95 % of branches in
  `burn_idle.c`. Unmeasured branches shall be flagged.
- **NFR-6.1** [Should]: The pure SM shall be adoptable by the
  `cyd2usb_st7789` profile without modification — the only required
  change for that profile is a new adapter (out of scope for this
  FSD but the design shall not preclude it).

### 4.3 Constraints

- **C-1** Wi-Fi must stay up in `BURN_IDLE_OFF` so the firmware can
  receive pushes; the ESP32-S3 therefore cannot enter deep sleep.
  Idle power savings come from the panel sleep, not from CPU /
  radio sleep.
- **C-2** Touch must be able to wake the host from the panel-off
  state without re-initialising the panel from scratch. The
  Waveshare 1.43" board does not route the FT3168 INT line to a
  GPIO, so we cannot drive touch wake via an interrupt; the LVGL
  indev callback polls the controller at ~30 Hz from LVGL's input
  timer, which keeps ticking independently of panel state. If LVGL
  ever stops ticking while the panel is off, the SM contract still
  holds — the adapter just loses the touch wake source.
- **C-3** The SH8601 brightness register has a finite resolution
  (typically 0–255). `brightness_pct` shall be converted to the
  device-specific range inside the driver, not inside the SM.

---

## 5. Risks, Assumptions & Dependencies

### 5.1 Technical Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| QMI8658 accel sampling at 21 Hz produces too much jitter at the motion threshold (false `EV_MOTION` storms) | Medium | High — would keep the panel pinned awake | Filter via per-axis low-pass before delta computation; expose threshold as Kconfig (FR-3.8) |
| `lv_display_set_rotation` is not honoured by the SH8601 driver path | Medium | Medium | Fall back to manual orientation transform in the LVGL flush callback; gated by Kconfig FR-1.6 |
| LVGL indev callback stops being scheduled while panel is in `BURN_IDLE_OFF`, breaking touch wake (C-2) | Low | Low — motion / push still wake | Confirmed during bring-up that LVGL's input timer is independent of panel state; if a future refactor changes this, disable touch as a wake source and document the deviation |
| Codex CLI changes the on-disk rate-limit storage format, or `rateLimits/read` against a long-lived app-server stops re-reading from disk | Low | Medium — poll would return stale values forever | Verify in CI / on bring-up against each `codex-cli` upgrade. Fallback design: periodically `_terminate()` the app-server subprocess so the next `_run_once` iteration's bootstrap re-reads from disk. |
| Existing colour tokens already violate FR-5 | Medium | Low | Phase 4 acceptance includes a one-time palette audit; treat violations as bugs and fix |

### 5.2 Assumptions

- **A-1** The QMI8658 is wired on the AMOLED-1.43 board exactly as
  shown in the Waveshare `03_I2C_QMI8658` reference demo. The
  driver in that demo is directly reusable.
- **A-2** The capacitive-touch controller (FT3168 on the Waveshare
  1.43" board) has **no INT line** routed to the ESP32-S3 — the
  vendor schematic ties it to `-1`. Touch is therefore polled via
  an LVGL pointer input device whose read callback also notifies
  the burn-in adapter on a rising-edge press. The constraint is
  that LVGL's input timer keeps ticking while the panel is in
  `BURN_IDLE_OFF` (confirmed during bring-up — LVGL's timer is
  independent of panel state). See FR-3.2 / C-2.
- **A-3** A board-level pushbutton exists and can drive a GPIO IRQ.
  If the only available button is the BOOT button (already used
  for factory reset in `factory_reset.c`), the adapter shall share
  it — a press is unambiguously a user interaction regardless of
  intent.
- **A-4** Cross-process emission of `account/rateLimits/updated`
  notifications is **unreliable** — verified via `lsof` against the
  long-lived `codex app-server` subprocess: zero file watchers on
  any `~/.codex/state_*.sqlite` path, only an empty kqueue. Rate
  limit changes that originate in other `codex` CLI processes
  therefore do not reach the daemon's app-server, and no
  `rateLimits/updated` notification is emitted. The daemon must
  poll (FR-4.2) and cannot rely on this notification stream.
- **A-5** `account/rateLimits/read` against a long-lived
  `codex app-server` instance re-reads the underlying disk cache on
  each call — verified empirically with codex-cli 0.133.0: held one
  app-server for 120 s, did two `read` calls bracketing real CLI
  activity, observed `primary.usedPercent` advance from 15 → 17
  inside the same process. If this property regresses in a future
  codex-cli release, the fallback in § 5.1 (recycle the subprocess
  via the existing reconnect machinery) applies.
- **A-6** The wire-format `used_pct` field is normalised to the
  0.0–1.0 range (per `docs/wire-format.md`). The Codex upstream
  `usedPercent` is typed `f64` in the openai/codex Rust source; in
  practice the ChatGPT-plan backend ships integer-valued percentages
  so byte-exact comparison still works.
- **A-8** Codex's `resetsAt` slides ~60 s per 60 s of wall-clock
  even with zero activity — verified empirically by holding one
  app-server and observing two consecutive poll responses for the
  same `usedPercent`:

      prev=[('primary', 0.01, 1779733577), ...]
      new =[('primary', 0.01, 1779733637), ...]

  `_anchor_resets_at` rewrites the fresh `resets_at` to the
  last-pushed value on per-session `used_pct` match so this drift
  doesn't fire the dedupe gate every poll and wake the firmware out
  of idle. The firmware compensates for the (now intentionally
  stale) stored `resets_at` by synthesising `now +
  window_duration_mins*60` at render time whenever the session is
  `rolling=true` and `used_pct ≤ 0.01`.
- **A-7** LVGL 9.x is in use; `LV_DRAW_SW_COMPLEX` and the
  anti-aliased font path are available.

### 5.3 External Dependencies

- Waveshare reference `03_I2C_QMI8658` demo (license: review before
  copying into the tree).
- Espressif `esp_lcd_sh8601` managed component (already a project
  dependency).
- ESP-IDF v6.0.1 GPIO ISR service, `esp_timer`, `httpd_*`.
- Python 3.11+, `httpx` (already a daemon dependency).
- Waveshare demo code under `docs/ESP32-S3-AMOLED-1.43-Demo`. Check
  for relevant reference examples there before writing custom
  bring-up code — the QMI8658, SH8601, and touch controllers all
  ship with vendor demos that document the working register
  sequences.

### 5.4 Known Gaps

- **G-1** Pixel-shift / orbit (catalog § 1.1) is not in scope here.
  Orientation rotation provides coarse spatial diversity but does
  not protect a device that sits in a single orientation. Flag for
  a follow-up FSD.
- **G-2** The CYD profile does not yet adopt the pure SM. NFR-6.1
  preserves the design's reusability; actual adoption is deferred.
- **G-3** No telemetry yet on how often the idle SM enters Dimmed
  vs. Off vs. how often pushes wake the panel. Worth adding to
  `GET /health` once the feature is shipping (separate task).
- **G-4** Multi-machine staleness: the daemon's poll reads PC A's
  *local* Codex cache. Rate-limit activity that happens on PC B is
  invisible to PC A's daemon — its panel will keep showing PC A's
  last-observed value even if the global account quota has moved.
  Possible mitigations are all out of scope (forced refresh prompts
  cost real tokens; direct OpenAI calls bypass `app-server` and
  break the no-tokens-consumed property; shared state across PCs
  needs cloud sync). Document the limitation and accept it for v1.
- **G-5** ~~Reconnected-device blind-spot in multi-screen setups.~~
  **RESOLVED** by the mDNS resilience series (see
  [`mdns-discovery-resilience-plan.html`](../mdns-discovery-resilience-plan.html)).
  The daemon (and the Claude statusline) no longer evict a device on
  transport / health failure — only `/summary` 401 may remove a
  pairing. Transport and health failures are healed in-place via one
  throttled `discover_all()` per `(agent, device_id)` cooldown (default
  60 s, configurable via `BURNSCOPE_MDNS_RECONCILE_COOLDOWN_S`), with
  the refreshed host committed through the update-only
  `host_cache.update_paired_device_host` so a stale browse cannot
  resurrect a concurrently-removed pairing. A returning display thus
  re-receives push and health traffic on the next cycle without any
  manual `burnscope pair`. Original concern preserved here for
  historical context; see deep review (post-#42) finding H-1.

---

## 6. Interface Specifications

### 6.1 External Interfaces

#### 6.1.1 HTTP (no change to wire surface)

`POST /summary` is unchanged at the wire level. The firmware adds an
internal side effect: every successful put emits `EV_PUSH` into the
idle SM, which soft-wakes the panel — from `BURN_IDLE_OFF` or
`BURN_IDLE_DIMMED` it lands in `BURN_IDLE_DIMMED`; from
`BURN_IDLE_ACTIVE` it refreshes the idle timer without changing
state (see FR-2.3 for the full transition rule). The HTTP
semantics, error codes, and response body are inherited from
`docs/fsd/firmware-fsd.md` § 6.1.

#### 6.1.2 I²C (IMU + Touch)

| Bus action                          | Notes |
|-------------------------------------|-------|
| QMI8658 init                        | Follows Waveshare demo. Sets accel ODR to `LowPower_21Hz`, ±2 g range. |
| QMI8658 accel sample (3× int16)     | Polled at ~21 Hz from a FreeRTOS task; converted to mg. |
| FT3168 touch poll (I²C, 0x38)       | Polled via an LVGL pointer input device (no INT line on this board). On a rising-edge press the indev read callback calls `burn_idle_adapter_notify_touch()`, which posts `EV_TOUCH`. |

#### 6.1.3 GPIO (Button)

| Pin                              | Notes |
|----------------------------------|-------|
| Onboard button (GPIO 0 / BOOT)   | Negedge ISR posts `EV_BUTTON` directly to the adapter's FreeRTOS event queue, with a 100 ms in-ISR debounce against `esp_timer_get_time()`. Coexists with `factory_reset.c`'s polling on the same pin. |

### 6.2 Internal Interfaces

#### 6.2.1 `burn_idle.h` (pure module)

```c
typedef enum {
    BURN_IDLE_ACTIVE = 0,
    BURN_IDLE_DIMMED,
    BURN_IDLE_OFF,
} burn_idle_state_t;

typedef enum {
    BURN_IDLE_EV_MOTION = 0,
    BURN_IDLE_EV_TOUCH,
    BURN_IDLE_EV_BUTTON,
    BURN_IDLE_EV_PUSH,
    BURN_IDLE_EV_TIME,
} burn_idle_event_t;

typedef struct {
    int64_t  dim_after_us;
    int64_t  off_after_us;
    uint8_t  active_brightness_pct;     // default 70
    uint8_t  dimmed_brightness_pct;     // default 20
    int16_t  motion_threshold_mg;       // default 50
} burn_idle_config_t;

typedef struct {
    burn_idle_state_t state;
    uint8_t           brightness_pct;
    bool              panel_on;
    bool              changed;
} burn_idle_output_t;

typedef struct {
    burn_idle_state_t  state;
    int64_t            last_activity_us;
    burn_idle_config_t cfg;
    /* Internal — adapter must not read or modify. Stores the previous
     * step's output so `output.changed` can be computed without the
     * caller having to remember the last state. */
    burn_idle_output_t _prev_output;
} burn_idle_t;

bool               burn_idle_config_valid(const burn_idle_config_t *cfg);
void               burn_idle_init(burn_idle_t *sm, burn_idle_config_t cfg);
burn_idle_output_t burn_idle_step(burn_idle_t *sm,
                                  burn_idle_event_t ev,
                                  int64_t now_us);
```

`burn_idle_config_valid` checks invariants (positive thresholds,
`off_after_us ≥ dim_after_us`, brightness ≤ 100, `dimmed ≤ active`,
non-negative motion threshold) and returns false on any violation.
`burn_idle_init` asserts on it internally, so adapters that build a
config dynamically (e.g. from Kconfig at boot) can pre-flight via this
predicate without risking an `assert()` crash on bad input.

The SM is single-threaded; the adapter is responsible for serialising
events (an event queue or a mutex held across `burn_idle_step`).

#### 6.2.2 Snapshot listener (adapter ↔ existing snapshot store)

The adapter registers a listener via
`snapshot_store_register_listener(on_push, NULL)`. `on_push` posts
an `EV_PUSH` event into the adapter's event queue. The existing
`snapshot.h` interface is unchanged.

#### 6.2.3 `AgentSnapshot.semantically_equal` (client)

```python
class AgentSnapshot:
    ...
    def semantically_equal(self, other: "AgentSnapshot | None") -> bool:
        """Return True iff `other` represents the same user-visible state.

        Compares `agent` and the per-session
        `(type, used_pct, resets_at, rolling, window_duration_mins)`
        tuples, sorted by `type` so session order is not significant.
        Ignores `captured_at` — that timestamp bumps every time the daemon
        re-reads, but does not reflect a user-visible change. Returns
        False if `other is None`.
        """
```

Byte-exact on numerics — no tolerance kwarg. Codex reports
`usedPercent` as a `f64` in the openai/codex Rust source; in
practice the ChatGPT-plan backend ships integer values so byte-exact
still holds, but the dedupe key is robust to that detail because
`_anchor_resets_at` papers over the unrelated `resets_at` drift
*before* this comparison runs.

### 6.3 Data Models / Schemas

`AgentSnapshot` is unchanged. `SessionSnapshot` gained two new wire
fields — `rolling: bool` and `window_duration_mins: int` — to let
the firmware distinguish rolling-vs-fixed windows and synthesise a
local countdown at idle (see `docs/wire-format.md` and
`docs/fsd/firmware-fsd.md` § 6.3). The semantic-equality helper is
a method on the existing dataclass — no
new types.

### 6.4 Commands / Opcodes

Not applicable.

---

## 7. Operational Procedures

### 7.1 Deployment / Flashing

No change to the existing firmware deployment workflow. Building the
AMOLED profile pulls in `burn_protection/` and the new adapter
automatically via the existing `displays/<name>/sources.cmake`
pattern.

### 7.2 First-time Provisioning

Unchanged. The idle SM starts in `BURN_IDLE_ACTIVE` at boot.

### 7.3 Normal Operation

- Idle SM ticks at 1 Hz; transitions to Dimmed at 5 min, Off at
  30 min (defaults).
- Direct user interactions (motion, touch, button) lift the panel
  to ACTIVE. A qualifying push *soft-wakes* the panel — from OFF or
  DIMMED it lands in DIMMED only; from ACTIVE it refreshes the idle
  timer without changing state (FR-2.3).
- Codex daemon polls `account/rateLimits/read` every 60 s and
  pushes only when the result differs from `_last_pushed_snapshot`.
  Steady state with no token usage = zero pushes, panel can sleep.
- The UI rotates to match the device's orientation as detected by
  the accelerometer.

### 7.4 Maintenance

- **Tuning idle thresholds:** edit Kconfig under
  `BurnScope display → AMOLED burn-in` and rebuild.
- **Tuning Codex poll cadence:** edit `POLL_INTERVAL_S` in
  `codex_daemon.py`. Lower = fresher panel at the cost of more
  `app-server` traffic; higher = more staleness.
- **Force a wake from the daemon side** (useful for testing):
  bump `usedPercent` upstream (run any real Codex prompt that
  burns ≥ 1 % of a window). The next poll detects the change and
  pushes.

### 7.5 Recovery

- **Panel stuck off after a wake event:** check that the adapter's
  event queue is being drained; check `EV_TIME` is firing at 1 Hz
  via the existing `esp_timer` diagnostics in `/health`.
- **Codex daemon polls but every iteration is reported as
  "unchanged"** (panel never refreshes after a real change): check
  that `account/rateLimits/read` against the long-lived app-server
  still re-reads from disk — held to be true per A-5, but verify
  on each codex-cli upgrade. Workaround: restart the daemon
  (`launchctl unload && load`) to force a fresh bootstrap. If
  reproducible across restarts, A-5 has regressed and the § 5.1
  fallback (periodic app-server respawn) applies.
- **Codex daemon never dedupes (all pushes go through):** verify
  that `_last_pushed_snapshot` is being updated only on push
  success; a bug there leaves it permanently `None`.

---

## 8. Verification & Validation

### 8.1 Phase 1 Verification — Pure SM (host)

| Test ID    | Feature                          | Procedure                                                                 | Success Criteria |
|------------|----------------------------------|---------------------------------------------------------------------------|------------------|
| SM-001     | Initial state                    | `burn_idle_init(sm, default)`; inspect.                                   | `state == ACTIVE`, `last_activity == 0`. |
| SM-002     | Dim transition timing            | Step `EV_TIME` at `dim_after - 1`, then `dim_after + 1`.                  | First step: ACTIVE. Second: DIMMED. |
| SM-003     | Off transition timing            | Continue from SM-002 to `off_after + 1`.                                  | State transitions to OFF. |
| SM-010     | Motion wakes from DIMMED         | Drive to DIMMED, then `EV_MOTION` at `dim_after + 30`.                    | State returns to ACTIVE; `brightness_pct == active`. |
| SM-011     | Motion wakes from OFF            | Drive to OFF, then `EV_MOTION`.                                           | State returns to ACTIVE; `panel_on == true`. |
| SM-012     | Touch wakes from DIMMED          | Drive to DIMMED, then `EV_TOUCH`.                                         | Wakes to ACTIVE. |
| SM-013     | Button wakes from OFF            | Drive to OFF, then `EV_BUTTON`.                                           | Wakes to ACTIVE. |
| SM-014     | Push soft-wakes from OFF to DIMMED | Drive to OFF, then `EV_PUSH`.                                           | State transitions to DIMMED; `brightness_pct == dimmed`; `panel_on == true`; `changed == true`. |
| SM-015     | Push from DIMMED extends dim phase | Drive to DIMMED, then `EV_PUSH` at `MIN(10)`. Then `EV_TIME` at `MIN(35) − SEC(1)` and `MIN(35) + SEC(1)`. | Push leaves state DIMMED. Pre-boundary tick still DIMMED. Post-boundary tick falls to OFF — `(off − dim)` later than the original OFF boundary. |
| SM-016     | Push from OFF falls to OFF after `(off − dim)` more silence | Drive to OFF, `EV_PUSH` at `MIN(30)+SEC(5)`. Then `EV_TIME` at `MIN(55)+SEC(4)` and `MIN(55)+SEC(6)`. | Push lands in DIMMED. Pre-boundary tick still DIMMED. Post-boundary tick falls to OFF. |
| SM-017     | Push from ACTIVE refreshes idle timer | At `MIN(4)`, drive to ACTIVE-still. `EV_PUSH` at `MIN(4)+SEC(30)`. Then `EV_TIME` at `MIN(9)`. | Push leaves state ACTIVE with `changed == false`. Subsequent tick at `MIN(9)` still ACTIVE (without the refresh, would be DIMMED). |
| SM-020     | Brightness lookup from config    | Configure `active=70, dimmed=20`; drive ACTIVE / DIMMED / OFF.            | Output brightness matches table; OFF → 0. |
| SM-021     | Brightness changes when cfg differs | Same as SM-020 with `active=50`.                                       | `brightness_pct == 50` in ACTIVE. |
| SM-030     | `changed` is true only on change | Two `EV_TIME` calls inside ACTIVE, well below `dim_after_us`.             | Both calls report `changed == false`. The SM records its post-init output as the baseline in `burn_idle_init`, so a steady-state step does not spuriously report change. |
| SM-031     | `changed` is true on transition  | `EV_TIME` crossing `dim_after`.                                           | `changed == true` exactly once. |
| SM-040     | Threshold edge — just below      | `EV_TIME` at `dim_after - 1`.                                             | State unchanged. |
| SM-041     | Threshold edge — exactly at      | `EV_TIME` at `dim_after`.                                                 | State transitions (spec uses `≥`). |
| SM-050     | Rapid event flapping             | Alternate `EV_TIME` and `EV_MOTION` at sub-second granularity.            | No oscillation; ACTIVE held; `changed` only when state truly changes. |
| SM-060     | Config edge: equal thresholds    | `dim_after_us == off_after_us`.                                           | On crossing, SM jumps ACTIVE → OFF (or DIMMED → OFF with both signalled as one transition). Document the chosen behaviour and test it. |
| SM-061     | Config edge: off before dim      | Invalid config: `off_after < dim_after`.                                  | `burn_idle_init` rejects (return code or assertion); test asserts the rejection. |
| SM-070     | No ESP-IDF includes              | Grep `burn_idle.c` for `esp_`, `freertos`, `lvgl`, `lv_`, `driver/`.      | No matches. |
| SM-071     | Host compile                     | Invoke host CMake target.                                                 | Builds and tests run outside ESP-IDF. |

### 8.2 Phase 2 Verification — Adapter + Codex Poll/Dedupe (hardware + client)

| Test ID    | Feature                          | Procedure                                                                 | Success Criteria |
|------------|----------------------------------|---------------------------------------------------------------------------|------------------|
| DIM-001    | Dim at 5 min                     | Set defaults; leave device untouched; observe.                            | Brightness drops to 20 % at 5 min ± 5 s. |
| DIM-002    | Off at 30 min                    | Continue from DIM-001.                                                    | Panel goes dark at 30 min ± 5 s. |
| WAKE-001   | Motion wake                      | Tap / lift device while OFF.                                              | Panel begins ramping toward 70 % within 200 ms and reaches it after the configured `BURNSCOPE_AMOLED_WAKE_FADE_MS` (default 300 ms → fully bright by ~500 ms total). |
| WAKE-002   | Touch wake                       | Tap screen while OFF.                                                     | Same fade profile as WAKE-001 — ramp begins within 200 ms, completes after `BURNSCOPE_AMOLED_WAKE_FADE_MS`. |
| WAKE-003   | Button wake                      | Press button while OFF.                                                   | Same fade profile as WAKE-001 — ramp begins within 200 ms, completes after `BURNSCOPE_AMOLED_WAKE_FADE_MS`. |
| WAKE-004   | Qualifying push soft-wake        | While OFF, run a Codex prompt that burns ≥ 1 % of a window.               | Within ≤ 60 s of activity, push fires and the panel begins ramping toward `dimmed_brightness_pct` within 200 ms of the push, reaching it after `BURNSCOPE_AMOLED_WAKE_FADE_MS`. State remains DIMMED until a direct interaction (motion / touch / button) lifts to ACTIVE, or `(off_after_us − dim_after_us)` of silence falls back to OFF. |
| FADE-001   | Mid-fade preemption              | Let the panel begin a dim-down (ACTIVE → DIMMED at the 5-min mark); within the first second of the fade, press the button. | The brightness ramp reverses direction smoothly from the in-flight value back up to `active_brightness_pct` — no visible jump back to full first. |
| WAKE-005   | Non-qualifying poll does NOT wake| While OFF, leave upstream untouched for 5 min.                            | Codex daemon issues no pushes; panel stays OFF. |
| POLL-001   | Poll fires on cadence             | Run daemon with `BURNSCOPE_LOG_LEVEL=DEBUG`; tail the log for 70 s with no upstream activity. | At least one "codex poll: rate limits unchanged; skipping push" debug line in the window. |
| POLL-002   | Poll no-ops before bootstrap     | Force `_client_id = None`; let `_poll_loop` run a few iterations.         | Zero calls to `_request`; queue stays empty. |
| POLL-003   | Poll tolerates request errors    | Inject a `CodexProtocolError` from `_request`; observe.                   | Loop continues; the *next* successful read enqueues correctly. |
| DEDUPE-001 | First-run always pushes          | Restart `codex_daemon` against a steady upstream.                         | First poll-driven push fires; `_last_pushed_snapshot` becomes non-None. |
| DEDUPE-002 | Identical snapshot drops         | Two consecutive `account/rateLimits/read` results with the same `usedPercent`. | Second produces no `POST /summary`; debug log shows dedupe drop. |
| DEDUPE-003 | Any `used_pct` delta pushes      | Inject `usedPercent` deltas of 1 and 5 percentage points.                 | Both push (byte-exact comparison; no tolerance). |
| DEDUPE-004 | Session set change pushes        | Inject a snapshot adding a new session `type`.                            | Push fires regardless of `used_pct`. |
| DEDUPE-005 | Resets-at-only drift is anchored, not pushed | Inject two snapshots with identical `used_pct` but `resets_at` advancing ~60 s per poll (codex's wall-clock-driven `resetsAt`). | Second produces no `POST /summary`; debug log shows dedupe drop. `_anchor_resets_at` rewrites the fresh value to match. |
| DEDUPE-008 | Real rollover (`used_pct` reset + new `resets_at`) | Inject a snapshot whose `used_pct` drops to 0 alongside a forward `resets_at` jump. | Push fires (anchor is gated on `used_pct` match, so a `used_pct` change always flows through). |
| DEDUPE-006 | Failed push does not advance state | Push transport-fails on all paired devices.                            | `_last_pushed_snapshot` unchanged; next poll re-attempts the same content. |
| DEDUPE-007 | Helper is unit-tested in isolation | `pytest client/tests/test_schema.py`.                                   | All `test_semantically_equal_*` cases pass. |

### 8.3 Phase 3 Verification — Orientation (hardware)

| Test ID      | Feature                        | Procedure                                                                | Success Criteria |
|--------------|--------------------------------|--------------------------------------------------------------------------|------------------|
| IMU-ROT-001  | Each quadrant rendered          | Hold device in each of 4 orientations for ≥ 2 s.                         | UI re-orients to match within 500 ms of stable orientation. |
| IMU-ROT-002  | Hysteresis at 45°               | Slowly tilt through 45°; pause.                                          | No flapping; rotation does not change mid-tilt. |
| IMU-ROT-003  | Debounce on quick flip          | Flip device through two quadrants in < 500 ms.                           | Final orientation only is applied (no intermediate flash). |
| IMU-ROT-004  | Disabled rotation Kconfig       | Build with FR-1.6 option off.                                            | UI stays at 0°; motion detection still functional. |

### 8.4 Phase 4 Verification — UI Constraints

| Test ID    | Feature                          | Procedure                                                                 | Success Criteria |
|------------|----------------------------------|---------------------------------------------------------------------------|------------------|
| PALETTE-001| Pure-white rejection             | Introduce a `#FFFFFF` token to the AMOLED palette and build.              | Build fails with a referenced FR-5.1 message. |
| PALETTE-002| Saturated-blue rejection         | Introduce `#0000FF`.                                                      | Build fails referencing FR-5.2. |
| PALETTE-003| Boundary value accepted          | Introduce `#C0C0BF` (max ≥ 0xC0 only in two channels).                    | Build passes. |
| PALETTE-004| Saturated-blue boundary          | Introduce `#3040B0`.                                                      | Build passes — blue ≥ 0xB0 but R ≥ 0x40. |
| AA-001     | Text feathering                  | Capture a screen-grab of body text; pixel-inspect the glyph edge.         | Edge has ≥ 1 intermediate-intensity pixel between full-foreground and full-background. |
| AA-002     | Arc feathering                   | Same for a gauge arc.                                                     | Edge feathered, not staircased. |
| AA-003     | Custom primitive feathering      | Inspect a hand-drawn divider.                                             | Edge pixel rendered at ~50 % intensity. |

### 8.5 Acceptance Tests

| Test ID | Feature                                  | Procedure                                                                   | Success Criteria |
|---------|------------------------------------------|------------------------------------------------------------------------------|------------------|
| AT-1    | 24 h soak with idle cycles               | Continuous operation; intermittent user activity simulated via accel taps. | No spurious wakes; no missed wakes; no panel-state leaks (Dimmed should not "stick" without a corresponding `last_activity` update). Free heap drift < 5 %. |
| AT-2    | Pure SM portability                       | Compile `burn_idle.c` against the CYD profile build (do not wire it up).   | Compiles cleanly with no profile-specific changes. |
| AT-3    | Codex steady-state silence                | 1 h with no upstream change.                                                | Daemon issues zero pushes; firmware logs no `EV_PUSH`; panel enters and stays in OFF. |

### 8.6 Live Verification Log

| Date       | Build                | Board        | Test     | Result | Notes |
|------------|----------------------|--------------|----------|--------|-------|
| 2026-05-26 | `9687070` (PR #48)   | AMOLED-1.43  | DIM-001  | ✅     | Compressed thresholds (`IDLE_DIM_MINUTES=1`); panel dropped from 70 % to 20 % at ~1 min idle. |
| 2026-05-26 | `9687070` (PR #48)   | AMOLED-1.43  | DIM-002  | ✅     | Compressed thresholds (`IDLE_OFF_MINUTES=2`); panel turned off at ~2 min idle. |
| 2026-05-26 | `9687070` (PR #48)   | AMOLED-1.43  | WAKE-002 | ✅     | Tap from OFF → wake within target window. Held finger does not re-post (rising-edge detection in `ui.c` indev callback). |
| 2026-05-26 | `9687070` (PR #48)   | AMOLED-1.43  | WAKE-003 | ✅     | Short BOOT button press from OFF → wake. GPIO 0 ISR coexists with `factory_reset.c` polling on the same pin. |
| 2026-05-26 | `9687070` (PR #48)   | AMOLED-1.43  | WAKE-004 | ⚠ supersed. | Verified under the old "PUSH wakes to ACTIVE" contract. Behavior changed to soft-wake-to-DIMMED in PR #50; see the next row for re-verification under the new contract. |
| 2026-05-26 | `7133687` (PR #50)   | AMOLED-1.43  | WAKE-004 | ✅     | New soft-wake contract verified on AMOLED-1.43: with compressed thresholds (`IDLE_DIM_MINUTES=1`, `IDLE_OFF_MINUTES=2`), the panel reached OFF, then a real `POST /summary` arrival lifted the panel to DIMMED (≈ 20 %), **not** ACTIVE (70 %). State remains DIMMED until a direct interaction lifts to ACTIVE or `(off_after_us − dim_after_us)` of silence falls back to OFF. Matches the FR-2.3 soft-wake contract. |
| 2026-05-26 | `7133687` (PR #50)   | AMOLED-1.43  | SM-015   | ✅     | Push-from-DIMMED extends the dim phase, verified on-hardware. With the panel sitting in DIMMED at 20 %, sustained `POST /summary` arrivals every <1 min kept the panel pinned at DIMMED (the SM emits no `output.changed` events because DIMMED → DIMMED is steady-state; verified visually by absence of dim-to-off transition for as long as pushes kept arriving). When pushes stopped, the panel fell to OFF ~1 min later (= `off_after_us − dim_after_us` from the last push), matching the FR-2.3 backdating math. |
| 2026-05-26 | `1b4f9c7` (PR #49)   | AMOLED-1.43  | WAKE-001 | ✅     | Motion wake — picking the device up from rest while OFF restored the panel within target window. The QMI8658 driver probes both 0x6A and 0x6B; this board responded at 0x6B. |
| 2026-05-26 | `1b4f9c7` (PR #49)   | AMOLED-1.43  | WAKE-005 | ✅     | Device left untouched on a desk for 5 min with no upstream change: Codex daemon issued no pushes (per `_anchor_resets_at` + `semantically_equal` dedupe), no spurious motion fires, panel stayed in OFF. |
| —          | —                    | —            | AT-1     | ⏳     | 24 h soak — pending. |

### 8.7 Traceability Matrix

| Requirement | Priority | Test Case(s)                                          | Status |
|-------------|----------|-------------------------------------------------------|--------|
| FR-1.1      | Must     | IMU-ROT-001, IMU-ROT-002                              | Planned |
| FR-1.2      | Must     | IMU-ROT-001                                           | Planned |
| FR-1.3      | Must     | IMU-ROT-002                                           | Planned |
| FR-1.4      | Must     | IMU-ROT-003                                           | Planned |
| FR-1.5      | Should   | Code inspection during IMU-ROT-001                    | Planned |
| FR-1.6      | May      | IMU-ROT-004                                           | Planned |
| FR-2.1      | Must     | SM-070, SM-071, AT-2                                  | Verified (host tests, commit `e163ecf`) |
| FR-2.2      | Must     | SM-001, SM-002, SM-003                                | Verified (host tests, commit `e163ecf`) |
| FR-2.3      | Must     | SM-010, SM-011, SM-012, SM-013, SM-014, SM-015, SM-016, SM-017 | Verified (host tests; soft-wake semantics added on `feat/push-soft-wake`) |
| FR-2.4      | Must     | SM-002, SM-003, SM-041                                | Verified (host tests, commit `e163ecf`) |
| FR-2.5      | Must     | SM-030, SM-031                                        | Verified (host tests, commit `e163ecf`) |
| FR-2.6      | Must     | SM-020, SM-021                                        | Verified (host tests, commit `e163ecf`) |
| FR-2.7      | Should   | SM-001                                                | Verified (host tests, commit `e163ecf`) |
| FR-3.1      | Must     | WAKE-001, DIM-001 (motion path absence keeps idle)    | Verified (WAKE-001 on `1b4f9c7`, DIM-001 on `9687070`) |
| FR-3.2      | Must     | WAKE-002                                              | Verified (`9687070`) |
| FR-3.3      | Must     | WAKE-003                                              | Verified (`9687070`) |
| FR-3.4      | Must     | WAKE-004                                              | Verified (`9687070`) |
| FR-3.5      | Must     | DIM-001, DIM-002                                      | Verified (`9687070`) |
| FR-3.6      | Must     | WAKE-001, DIM-002                                     | Verified (motion path on `1b4f9c7`, off-transition on `9687070`) |
| FR-3.7      | Must     | WAKE-004, AT-3 (HTTP keeps running)                   | Verified end-to-end via WAKE-004 (`9687070`); AT-3 pending soak |
| FR-3.8      | Should   | Build-config inspection during DIM-001                | Verified (Kconfig defaults reachable via `idf.py menuconfig`) |
| FR-4.1      | Must     | DEDUPE-001                                            | Verified (commits `9a5a443`, `b7b2492`, PRs #41 + #46) |
| FR-4.2      | Must     | POLL-001, POLL-002, POLL-003                          | Verified (`b7b2492`) |
| FR-4.3      | Must     | DEDUPE-002, DEDUPE-003                                | Verified (`b7b2492`) |
| FR-4.4      | Must     | DEDUPE-003 (byte-exact, no tolerance)                 | Verified (`semantically_equal`, `b7b2492`) |
| FR-4.5      | Must     | DEDUPE-004, DEDUPE-005                                | Verified (`_anchor_resets_at`, `b7b2492`) |
| FR-4.6      | Must     | DEDUPE-006                                            | Verified (`b7b2492`) |
| FR-4.7      | Should   | POLL-001 (cadence patchable via constant)             | Verified (POLL_INTERVAL_S constant in `codex_daemon.py`) |
| FR-4.8      | Should   | Code inspection — `_dispatch` notification path retained | Verified (`b7b2492`) |
| FR-4.9      | May     | DEDUPE-007 (helper is agent-agnostic)                 | Verified (`semantically_equal` lives in `schema.py`, agent-agnostic) |
| FR-5.1      | Must     | PALETTE-001, PALETTE-003                              | Planned |
| FR-5.2      | Must     | PALETTE-002, PALETTE-004                              | Planned |
| FR-5.3      | Should   | PALETTE-001 (build fail proves enforcement)           | Planned |
| FR-6.1      | Must     | AA-001                                                | Planned |
| FR-6.2      | Must     | AA-002                                                | Planned |
| FR-6.3      | Should   | AA-003                                                | Planned |
| NFR-1.1     | Must     | SM-002 with cycle-count instrumentation (host or target) | Planned |
| NFR-1.2     | Should   | CPU profiler measurement during AT-1                  | Planned |
| NFR-2.1     | Must     | WAKE-001 / WAKE-004 with stopwatch / oscilloscope     | Verified qualitatively (`1b4f9c7` / `9687070`); stopwatch/scope measurement pending |
| NFR-2.2     | Should   | Visual inspection during DIM-001/DIM-002              | Verified (`9687070`) |
| NFR-3.1     | Must     | AT-3                                                  | Planned (AT-3 soak still pending) |
| NFR-3.2     | Should   | Microbenchmark during DEDUPE-007                      | Planned |
| NFR-4.1     | Must     | SM-071                                                | Verified (host purity check, commit `e163ecf`) |
| NFR-5.1     | Should   | Coverage report from SM-* host tests                  | Planned |
| NFR-6.1     | Should   | AT-2                                                  | Verified (host harness builds `burn_idle.c` without profile-specific changes, `e163ecf`) |

---

## 9. Troubleshooting Guide

| Symptom                                              | Likely Cause                                                      | Diagnostic Steps                                                              | Corrective Action |
|------------------------------------------------------|-------------------------------------------------------------------|-------------------------------------------------------------------------------|-------------------|
| Panel never dims                                     | Codex daemon pushing on every poll (dedupe regressed)             | Tail daemon at debug; expect "rate limits unchanged; skipping push" in steady state. | Re-check `_last_pushed_snapshot` is updated when *any* device's `PushResult.ok == True` (FR-4.6 — `any_ok`, not `overall_ok`). |
| Panel dims but never turns off                       | `EV_TIME` not firing at 1 Hz                                       | Add `ESP_LOGD` in `EV_TIME` handler; verify timer running.                    | Re-arm `esp_timer`; check timer queue depth. |
| Panel turns off and never comes back                 | Wake source unwired                                                | Manual motion test (WAKE-001) and touch test (WAKE-002).                      | Inspect adapter event queue and ISR registration. |
| UI rotates wildly                                    | Hysteresis band too narrow or noisy accel                          | Inspect raw accel samples; widen `motion_threshold_mg` and rotation hysteresis. | Tune Kconfig. |
| Daemon polls but panel never refreshes after real activity | `rateLimits/read` against long-lived app-server stopped re-reading from disk (A-5 regression) | Restart the daemon (`launchctl unload && load`); if a fresh bootstrap shows up-to-date values but subsequent polls drift, A-5 has regressed. | Apply the § 5.1 fallback: periodically respawn the app-server via the existing `_terminate()` + restart machinery. |
| Build fails with palette error                       | A colour token violates FR-5                                       | Read the build-error file/line.                                               | Replace the colour with a compliant value. |

---

## 10. Appendix

### 10.1 Constants & Defaults

| Constant                            | Default                       | Source |
|-------------------------------------|-------------------------------|--------|
| `IDLE_DIM_MINUTES`                  | 5                             | A2 source spec |
| `IDLE_OFF_MINUTES`                  | 30                            | A2 source spec |
| `DEFAULT_BRIGHTNESS`                | 70 %                          | A3 source spec |
| `IDLE_BRIGHTNESS`                   | 20 %                          | A2 source spec |
| Motion threshold                    | 50 mg                         | A2 source spec |
| Rotation hysteresis                 | 0.2 g                         | A1 source spec |
| Rotation debounce                   | 500 ms                        | A1 source spec |
| Accel ODR                           | `Qmi8658AccOdr_LowPower_21Hz` | A1 source spec |
| `POLL_INTERVAL_S` (client)          | 60 s                          | This FSD |
| SM tick cadence                     | 1 Hz                          | This FSD |
| Palette: max channel cap            | 0xC0 per channel              | A4 source spec |
| Palette: saturated-blue rule        | B ≥ 0xB0 AND R < 0x40 AND G < 0x40 → reject | A4 source spec |

### 10.2 Example Idle Sequence (SM trace)

```
t=0       init                      (no output; baseline = ACTIVE, br=70, panel=on)
t=60 s    EV_TIME                   → ACTIVE, br=70, panel=on, changed=0
t=5 min   EV_TIME                   → DIMMED, br=20, panel=on, changed=1
t=10 min  EV_TIME                   → DIMMED, br=20, panel=on, changed=0
t=30 min  EV_TIME                   → OFF,    br=0,  panel=off, changed=1
t=30:05   EV_PUSH                   → DIMMED, br=20, panel=on, changed=1
                                      (soft-wake: last_activity_us = t=30:05 − 5 min = t=25:05)
t=30:06   EV_TIME                   → DIMMED, br=20, panel=on, changed=0
t=55:05   EV_TIME                   → OFF,    br=0,  panel=off, changed=1
                                      (= last_activity_us + off_after_us = t=25:05 + 30 min)
```

`burn_idle_init` returns `void` — it records `{ACTIVE, br=70, panel=on}`
as the internal baseline so the very first `burn_idle_step` can
compute `changed` honestly against the post-init state.

### 10.3 Source-of-Truth Crosswalk

| Section ID in source spec                        | This FSD               |
|--------------------------------------------------|------------------------|
| `oled_burnin_mitigation.md` A1 (rotation)        | FR-1.x                 |
| `oled_burnin_mitigation.md` A2 firmware side     | FR-2.x, FR-3.x         |
| `oled_burnin_mitigation.md` A2 client side       | FR-4.x                 |
| `oled_burnin_mitigation.md` A2 implementation    | FR-2.1, NFR-4.1, NFR-6.1 |
| `oled_burnin_mitigation.md` A3 (brightness)      | FR-3.6, FR-3.8, FR-2.6 |
| `oled_burnin_mitigation.md` A4 (palette)         | FR-5.x                 |
| `oled_burnin_mitigation.md` A5 (soft edges)      | FR-6.x                 |

---

## 11. Related

- `[[burnscope/docs/oled_burnin_mitigation.md]]` — catalog + AMOLED concrete decisions; source of truth for design defaults.
- `[[burnscope/docs/fsd/firmware-fsd.md]]` — existing firmware FSD; `POST /summary` and snapshot store inherited from here.
- `[[burnscope/docs/wire-format.md]]` — `AgentSnapshot` shape; basis of FR-4 semantic equality.
- `[[burnscope/docs/client-spec-v2.html]]` — multi-agent client architecture; locates the Codex daemon.
- `[[burnscope/docs/codex-app-server.html]]` — `account/rateLimits/read` and `rateLimits/updated` semantics (basis of A-4, A-5, FR-4).
- `[[burnscope/CLAUDE.md]]` — repository conventions.
- `[[burnscope/docs/ESP32-S3-AMOLED-1.43-Demo]]` - Waveshare demo code for ESP32-S3-AMOLED-1.43.
