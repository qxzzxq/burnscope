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
3. **Codex daemon pushes once a minute regardless of user activity** —
   would defeat A2 if treated as a wake signal. Solved by client-side
   push **dedupe** so pushes correlate with semantic change.
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
- **Codex daemon (`codex_daemon.py`):** owns push dedupe. Must
  produce a push only on semantic change so the firmware's idle
  state machine can sleep.
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
- Codex daemon pushes only when an `AgentSnapshot` differs from the
  last successfully pushed snapshot, under a small numeric tolerance
  (FR-4.x).
- The AMOLED profile dims at 5 min idle and turns off at 30 min idle
  (defaults; both Kconfig-configurable). Any of accelerometer
  motion, touch, button, or qualifying `POST /summary` wakes the
  panel immediately (FR-2.x, FR-3.x).
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
Codex app-server ─►──── account/rateLimits/updated ─►── AgentSnapshot ──► │
                          │                                                │
                          │  semantically_equal(new, _last_pushed)?        │
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
| **Orientation detector**       | `firmware/main/displays/amoled_sh8601/orientation.c` | Reads accelerometer gravity vector, picks quadrant with hysteresis + debounce, drives `lv_disp_set_rotation`. |
| **Codex snapshot dedupe**      | `client/src/burnscope_client/codex_daemon.py` (modified) + `client/src/burnscope_client/schema.py` (helper) | `AgentSnapshot.semantically_equal(other)` and a new `_last_pushed_snapshot` field gating `_enqueue_snapshot`. |
| **Palette validator**          | `firmware/main/displays/amoled_sh8601/palette_check.c` (or CMake-time script) | Static check that all colour tokens used by the AMOLED UI satisfy A4. |

### 2.2 Hardware / Platform Architecture

| Element            | Detail                                                                                        |
|--------------------|-----------------------------------------------------------------------------------------------|
| MCU                | ESP32-S3 (Waveshare ESP32-S3-Touch-AMOLED-1.43 module)                                        |
| Panel              | 466×466 round AMOLED, SH8601 / CO5300 controller, QSPI                                        |
| IMU                | QMI8658 6-axis (only the accelerometer is used for this feature)                              |
| Touch              | Capacitive touch controller, CST816-class, INT line wired to GPIO                             |
| Button             | Onboard pushbutton (board-specific GPIO; reuse the BOOT-button long-press wiring conceptually) |
| Bus                | I²C for IMU and touch; QSPI for panel                                                         |
| Brightness control | SH8601 brightness register via `esp_lcd_panel_io_tx_param`                                    |

### 2.3 Software Architecture

```
firmware/main/
├── burn_protection/
│   ├── burn_idle.h            # pure SM API
│   ├── burn_idle.c            # pure logic, <stdint.h> + <stdbool.h> only
│   ├── Kconfig                # tunables: thresholds, default brightness
│   └── test/
│       ├── burn_idle_test.c   # host unit tests (Unity-style)
│       └── CMakeLists.txt     # host-only target
└── displays/amoled_sh8601/
    ├── driver.c               # existing — SH8601 bring-up
    ├── ui.c                   # existing — LVGL layout
    ├── orientation.c          # NEW — accel → quadrant → lv_disp_set_rotation
    ├── burn_idle_adapter.c    # NEW — events + outputs ↔ hardware
    └── palette_check.c        # NEW (or build-time .py) — token validator

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

### 3.2 Phase 2 — AMOLED Adapter + Codex Dedupe (paired)

**Scope.** The end-to-end idle behaviour on the AMOLED hardware,
together with the upstream change that makes it actually reachable.
These two land together because neither is end-to-end-verifiable on
its own.

**Deliverables.**

- `burn_idle_adapter.c`: IMU sampler (with motion thresholding),
  touch IRQ binding, button IRQ binding, 1 Hz `esp_timer` for
  `EV_TIME`, snapshot listener hooked to `EV_PUSH`, brightness +
  panel sleep/wake actions on SM output.
- `schema.py`: `AgentSnapshot.semantically_equal(other, *,
  usage_percent_tolerance=0.5)` (or a free function — see § 6.3).
- `codex_daemon.py`: new `_last_pushed_snapshot` member; dedupe in
  `_enqueue_snapshot` (or just before `_push_one` enqueues);
  `_last_pushed_snapshot` updated only after a successful push;
  always push on first run; debug log on dedupe drop.

**Exit criteria.** All Phase 2 tests in § 8.2 pass on hardware:
manual dim/off timing, all four wake sources, push-with-no-change
does not wake, push-with-change does wake.

**Dependencies.** Phase 1.

### 3.3 Phase 3 — Orientation Rotation

**Scope.** Read accelerometer; rotate the LVGL display in 90°
quadrants with hysteresis + debounce.

**Deliverables.**

- `orientation.c`: low-power accel sampling, dominant-axis selector
  with ±0.2 g hysteresis band, 500 ms debounce, calls into LVGL's
  rotation API and into the framebuffer flush pipeline (verify
  `lv_disp_set_rotation` is sufficient for the SH8601 driver — fall
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
  `EV_MOTION`, `EV_TOUCH`, `EV_BUTTON`, `EV_PUSH`, and `EV_TIME`.
  All four non-time events shall reset the `last_activity` timestamp
  and transition the SM to `BURN_IDLE_ACTIVE`.
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
- **FR-3.2** [Must]: The adapter shall bind the capacitive-touch
  controller's interrupt line to a handler that emits `EV_TOUCH`.
- **FR-3.3** [Must]: The adapter shall bind the on-board button to
  a handler that emits `EV_BUTTON` on press.
- **FR-3.4** [Must]: The adapter shall register a snapshot listener
  via `snapshot_store_register_listener` and emit `EV_PUSH` for
  every snapshot put. (The Codex daemon's dedupe — FR-4.x —
  guarantees these are semantically meaningful.)
- **FR-3.5** [Must]: The adapter shall run a 1 Hz `esp_timer` that
  calls `burn_idle_step(sm, EV_TIME, now_us)` once per second.
- **FR-3.6** [Must]: On any `output.changed == true` step, the
  adapter shall apply `brightness_pct` to the SH8601 brightness
  register and, on `panel_on` transitions, call panel sleep or
  wake. Brightness changes shall be instantaneous (no fade-up on
  wake) per the source spec.
- **FR-3.7** [Must]: While in `BURN_IDLE_OFF`, the HTTP server,
  Wi-Fi, mDNS, and snapshot store shall continue to operate; an
  incoming `POST /summary` shall both update the framebuffer and
  emit `EV_PUSH`, restoring the panel.
- **FR-3.8** [Should]: The motion threshold, dim threshold, off
  threshold, default brightness, and dimmed brightness shall be
  exposed as Kconfig options under `BurnScope display → AMOLED
  burn-in`.

**FR-4 Codex Daemon Push Dedupe**

- **FR-4.1** [Must]: `codex_daemon.py` shall maintain a
  `_last_pushed_snapshot` attribute, initialised to `None`.
- **FR-4.2** [Must]: Before enqueuing a freshly-derived
  `AgentSnapshot` for push, the daemon shall compare it against
  `_last_pushed_snapshot` using a semantic-equality predicate
  (FR-4.4 / FR-4.5).
- **FR-4.3** [Must]: If the predicate reports equal, the snapshot
  shall not be enqueued and shall not be pushed; the event shall be
  logged at debug level. If unequal (or if
  `_last_pushed_snapshot is None`), the snapshot shall be enqueued
  for the existing `_pusher_loop`.
- **FR-4.4** [Must]: The semantic-equality predicate shall be
  insensitive to `resets_at` ticking forward, and shall consider
  `used_pct` equal when `|new - old| < USAGE_PERCENT_TOLERANCE`,
  where `USAGE_PERCENT_TOLERANCE = 0.005` (0.5 percentage points
  expressed in the 0.0–1.0 range used by `SessionSnapshot`).
- **FR-4.5** [Must]: For non-numeric fields (`agent`,
  `sessions[*].type`, and the set of `(type)` keys present), the
  predicate shall require byte-exact equality. A change in the set
  of session types is by definition a state transition.
- **FR-4.6** [Must]: `_last_pushed_snapshot` shall be updated only
  after a push completes with `result.ok == True`. A transport
  failure or auth drop shall leave the previous value untouched so
  the next opportunity re-pushes the same content.
- **FR-4.7** [Should]: The tolerance and the field list shall be
  named constants in `schema.py` so they are testable and tunable
  in isolation.
- **FR-4.8** [May]: The dedupe predicate may also be reused by a
  future Claude-statusline dedupe path; the helper shall not depend
  on Codex-specific state.

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
- **NFR-2.1** [Must]: Wake latency from a wake event (motion,
  touch, button, or `EV_PUSH`) to brightness restored to
  `DEFAULT_BRIGHTNESS` shall be ≤ 200 ms at the 95th percentile.
- **NFR-2.2** [Should]: Dim-to-Off transition shall be visually
  noticeable but not abrupt; the SH8601 brightness register write
  is the only required action (no fade ramp). If a smoother visual
  is required later, it shall be added in the adapter, not the SM.
- **NFR-3.1** [Must]: The Codex daemon's dedupe path shall add
  ≤ 5 ms to the per-snapshot processing time, measured on
  reference hardware (Apple Silicon laptop, Python 3.11).
- **NFR-3.2** [Should]: In steady-state operation where upstream
  rate-limit values are unchanged, the daemon shall produce zero
  pushes per minute (modulo the first-run push).
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
  CST816-class controller's IRQ remains active across SH8601 sleep.
  If this is later disproven, the SM contract still holds — the
  adapter just loses the touch wake source.
- **C-3** The SH8601 brightness register has a finite resolution
  (typically 0–255). `brightness_pct` shall be converted to the
  device-specific range inside the driver, not inside the SM.

---

## 5. Risks, Assumptions & Dependencies

### 5.1 Technical Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| QMI8658 accel sampling at 21 Hz produces too much jitter at the motion threshold (false `EV_MOTION` storms) | Medium | High — would keep the panel pinned awake | Filter via per-axis low-pass before delta computation; expose threshold as Kconfig (FR-3.8) |
| `lv_disp_set_rotation` is not honoured by the SH8601 driver path | Medium | Medium | Fall back to manual orientation transform in the LVGL flush callback; gated by Kconfig FR-1.6 |
| Touch IRQ does not fire while panel is in sleep (C-2) | Medium | Low — motion / push still wake | Disable touch as a wake source; document the deviation |
| Codex `account/rateLimits/updated` semantics jitter `used_pct` by > 0.5 pp in steady state | Low | Medium — would defeat dedupe | Tighten or widen `USAGE_PERCENT_TOLERANCE`; constant lives in `schema.py` (FR-4.7) |
| Existing colour tokens already violate FR-5 | Medium | Low | Phase 4 acceptance includes a one-time palette audit; treat violations as bugs and fix |

### 5.2 Assumptions

- **A-1** The QMI8658 is wired on the AMOLED-1.43 board exactly as
  shown in the Waveshare `03_I2C_QMI8658` reference demo. The
  driver in that demo is directly reusable.
- **A-2** The capacitive-touch controller IRQ is wired to a GPIO
  with INTR capability and is supported by the ESP-IDF GPIO ISR
  service.
- **A-3** A board-level pushbutton exists and can drive a GPIO IRQ.
  If the only available button is the BOOT button (already used
  for factory reset in `factory_reset.c`), the adapter shall share
  it — a press is unambiguously a user interaction regardless of
  intent.
- **A-4** The Codex app-server emits `account/rateLimits/updated`
  with frequency on the order of one per minute when the daemon
  is connected. (Source: `codex_daemon.py` operational notes.)
- **A-5** The wire-format `used_pct` field has been normalised to
  the 0.0–1.0 range (per `docs/wire-format.md`), so the 0.5 pp
  tolerance expressed as 0.005 in that range is correct.
- **A-6** LVGL 9.x is in use; `LV_DRAW_SW_COMPLEX` and the
  anti-aliased font path are available.

### 5.3 External Dependencies

- Waveshare reference `03_I2C_QMI8658` demo (license: review before
  copying into the tree).
- Espressif `esp_lcd_sh8601` managed component (already a project
  dependency).
- ESP-IDF v6.0.1 GPIO ISR service, `esp_timer`, `httpd_*`.
- Python 3.11+, `httpx` (already a daemon dependency).
- Waveshare demo code in `docs/ESP32-S3-AMOLED-1.43-Demo`. Before trying to invent the wheel, we must check if there is any ref examples.

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

---

## 6. Interface Specifications

### 6.1 External Interfaces

#### 6.1.1 HTTP (no change to wire surface)

`POST /summary` is unchanged at the wire level. The firmware adds an
internal side effect: every successful put emits `EV_PUSH` into the
idle SM, which restores `BURN_IDLE_ACTIVE` and full brightness. The
HTTP semantics, error codes, and response body are inherited from
`docs/fsd/firmware-fsd.md` § 6.1.

#### 6.1.2 I²C (IMU + Touch)

| Bus action                          | Notes |
|-------------------------------------|-------|
| QMI8658 init                        | Follows Waveshare demo. Sets accel ODR to `LowPower_21Hz`, ±2 g range. |
| QMI8658 accel sample (3× int16)     | Polled at ~21 Hz from a FreeRTOS task; converted to mg. |
| Touch IRQ                           | Edge-triggered GPIO interrupt; ISR dispatches to a task that emits `EV_TOUCH`. |

#### 6.1.3 GPIO (Button)

| Pin                              | Notes |
|----------------------------------|-------|
| Onboard button (board-specific)  | Falling-edge interrupt → task → `EV_BUTTON`. |

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
    int64_t           last_activity_us;
    burn_idle_config_t cfg;
} burn_idle_t;

typedef struct {
    burn_idle_state_t state;
    uint8_t           brightness_pct;
    bool              panel_on;
    bool              changed;
} burn_idle_output_t;

void               burn_idle_init(burn_idle_t *sm, burn_idle_config_t cfg);
burn_idle_output_t burn_idle_step(burn_idle_t *sm,
                                  burn_idle_event_t ev,
                                  int64_t now_us);
```

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
    def semantically_equal(
        self,
        other: "AgentSnapshot | None",
        *,
        usage_percent_tolerance: float = USAGE_PERCENT_TOLERANCE,
    ) -> bool:
        """Return True iff `other` represents the same user-visible state.

        Compares `agent`, the set of session `type` keys, and per-type
        `used_pct` within `usage_percent_tolerance` (default 0.005,
        i.e. 0.5 percentage points). Ignores `captured_at` and
        `resets_at`. Returns False if `other` is None.
        """
```

`USAGE_PERCENT_TOLERANCE = 0.005` lives at module scope in
`schema.py`.

### 6.3 Data Models / Schemas

`AgentSnapshot` and `SessionSnapshot` are unchanged on the wire (see
`docs/wire-format.md` and `docs/fsd/firmware-fsd.md` § 6.3). The
semantic-equality helper is a method on the existing dataclass — no
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
- Any of motion, touch, button, or qualifying push wakes the panel.
- Codex daemon dedupes pushes — when nothing changes upstream,
  nothing reaches the panel, and the panel can sleep.
- The UI rotates to match the device's orientation as detected by
  the accelerometer.

### 7.4 Maintenance

- **Tuning idle thresholds:** edit Kconfig under
  `BurnScope display → AMOLED burn-in` and rebuild.
- **Tuning Codex dedupe sensitivity:** edit
  `USAGE_PERCENT_TOLERANCE` in `schema.py`.
- **Force a wake from the daemon side** (useful for testing):
  any code path that bumps `used_pct` by ≥ 0.5 pp (e.g. a
  scratched test fixture) will push and wake the panel.

### 7.5 Recovery

- **Panel stuck off after a wake event:** check that the adapter's
  event queue is being drained; check `EV_TIME` is firing at 1 Hz
  via the existing `esp_timer` diagnostics in `/health`.
- **Codex daemon never dedupes (all pushes go through):** verify
  that `_last_pushed_snapshot` is being updated only after
  `result.ok`; a bug there leaves it permanently `None`.

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
| SM-014     | Push wakes from OFF              | Drive to OFF, then `EV_PUSH`.                                             | Wakes to ACTIVE. |
| SM-020     | Brightness lookup from config    | Configure `active=70, dimmed=20`; drive ACTIVE / DIMMED / OFF.            | Output brightness matches table; OFF → 0. |
| SM-021     | Brightness changes when cfg differs | Same as SM-020 with `active=50`.                                       | `brightness_pct == 50` in ACTIVE. |
| SM-030     | `changed` is true only on change | Two `EV_TIME` calls inside ACTIVE.                                        | First call may set `changed=true` (init), second false. |
| SM-031     | `changed` is true on transition  | `EV_TIME` crossing `dim_after`.                                           | `changed == true` exactly once. |
| SM-040     | Threshold edge — just below      | `EV_TIME` at `dim_after - 1`.                                             | State unchanged. |
| SM-041     | Threshold edge — exactly at      | `EV_TIME` at `dim_after`.                                                 | State transitions (spec uses `≥`). |
| SM-050     | Rapid event flapping             | Alternate `EV_TIME` and `EV_MOTION` at sub-second granularity.            | No oscillation; ACTIVE held; `changed` only when state truly changes. |
| SM-060     | Config edge: equal thresholds    | `dim_after_us == off_after_us`.                                           | On crossing, SM jumps ACTIVE → OFF (or DIMMED → OFF with both signalled as one transition). Document the chosen behaviour and test it. |
| SM-061     | Config edge: off before dim      | Invalid config: `off_after < dim_after`.                                  | `burn_idle_init` rejects (return code or assertion); test asserts the rejection. |
| SM-070     | No ESP-IDF includes              | Grep `burn_idle.c` for `esp_`, `freertos`, `lvgl`, `lv_`, `driver/`.      | No matches. |
| SM-071     | Host compile                     | Invoke host CMake target.                                                 | Builds and tests run outside ESP-IDF. |

### 8.2 Phase 2 Verification — Adapter + Codex Dedupe (hardware + client)

| Test ID    | Feature                          | Procedure                                                                 | Success Criteria |
|------------|----------------------------------|---------------------------------------------------------------------------|------------------|
| DIM-001    | Dim at 5 min                     | Set defaults; leave device untouched; observe.                            | Brightness drops to 20 % at 5 min ± 5 s. |
| DIM-002    | Off at 30 min                    | Continue from DIM-001.                                                    | Panel goes dark at 30 min ± 5 s. |
| WAKE-001   | Motion wake                      | Tap / lift device while OFF.                                              | Panel restores to 70 % within 200 ms. |
| WAKE-002   | Touch wake                       | Tap screen while OFF.                                                     | Panel restores within 200 ms. |
| WAKE-003   | Button wake                      | Press button while OFF.                                                   | Panel restores within 200 ms. |
| WAKE-004   | Qualifying push wake             | While OFF, force a `used_pct` change ≥ 0.5 pp upstream.                   | Push fires, panel restores within 200 ms. |
| WAKE-005   | Non-qualifying push does NOT wake| While OFF, leave upstream values constant for 5 min.                      | Codex daemon issues no pushes; panel stays OFF. |
| DEDUPE-001 | First-run always pushes          | Restart `codex_daemon`, observe first push.                               | Push fires; `_last_pushed_snapshot` becomes non-None. |
| DEDUPE-002 | Identical snapshot drops         | Force two identical `rateLimits` payloads upstream.                       | Second produces no `POST /summary`; debug log shows dedupe drop. |
| DEDUPE-003 | Tolerance window                 | `used_pct` deltas of 0.001, 0.004, 0.006.                                 | First two: drop. Third: push. |
| DEDUPE-004 | Session set change pushes        | Add a new session `type` upstream.                                        | Push fires regardless of `used_pct`. |
| DEDUPE-005 | Auth / agent change pushes       | Simulate an `agent` field change (synthetic test).                        | Push fires. |
| DEDUPE-006 | Failed push does not advance state | Force a transport error on the next push.                              | `_last_pushed_snapshot` unchanged; retry pushes the same content. |
| DEDUPE-007 | Helper is unit-tested in isolation | `pytest client/tests/test_schema_semantic_equal.py`.                    | All cases pass. |

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

(To be populated on first AMOLED bring-up. Use the same ✅ / ⏳ / ❌
convention as `docs/fsd/firmware-fsd.md` § 8.4.)

### 8.7 Traceability Matrix

| Requirement | Priority | Test Case(s)                                          | Status |
|-------------|----------|-------------------------------------------------------|--------|
| FR-1.1      | Must     | IMU-ROT-001, IMU-ROT-002                              | Planned |
| FR-1.2      | Must     | IMU-ROT-001                                           | Planned |
| FR-1.3      | Must     | IMU-ROT-002                                           | Planned |
| FR-1.4      | Must     | IMU-ROT-003                                           | Planned |
| FR-1.5      | Should   | Code inspection during IMU-ROT-001                    | Planned |
| FR-1.6      | May      | IMU-ROT-004                                           | Planned |
| FR-2.1      | Must     | SM-070, SM-071, AT-2                                  | Planned |
| FR-2.2      | Must     | SM-001, SM-002, SM-003                                | Planned |
| FR-2.3      | Must     | SM-010, SM-011, SM-012, SM-013, SM-014                | Planned |
| FR-2.4      | Must     | SM-002, SM-003, SM-041                                | Planned |
| FR-2.5      | Must     | SM-030, SM-031                                        | Planned |
| FR-2.6      | Must     | SM-020, SM-021                                        | Planned |
| FR-2.7      | Should   | SM-001                                                | Planned |
| FR-3.1      | Must     | WAKE-001, DIM-001 (motion path absence keeps idle)    | Planned |
| FR-3.2      | Must     | WAKE-002                                              | Planned |
| FR-3.3      | Must     | WAKE-003                                              | Planned |
| FR-3.4      | Must     | WAKE-004                                              | Planned |
| FR-3.5      | Must     | DIM-001, DIM-002                                      | Planned |
| FR-3.6      | Must     | WAKE-001, DIM-002                                     | Planned |
| FR-3.7      | Must     | WAKE-004, AT-3 (HTTP keeps running)                   | Planned |
| FR-3.8      | Should   | Build-config inspection during DIM-001                | Planned |
| FR-4.1      | Must     | DEDUPE-001                                            | Planned |
| FR-4.2      | Must     | DEDUPE-002                                            | Planned |
| FR-4.3      | Must     | DEDUPE-002, DEDUPE-003                                | Planned |
| FR-4.4      | Must     | DEDUPE-003                                            | Planned |
| FR-4.5      | Must     | DEDUPE-004, DEDUPE-005                                | Planned |
| FR-4.6      | Must     | DEDUPE-006                                            | Planned |
| FR-4.7      | Should   | DEDUPE-007                                            | Planned |
| FR-4.8      | May      | DEDUPE-007 (helper is agent-agnostic)                 | Planned |
| FR-5.1      | Must     | PALETTE-001, PALETTE-003                              | Planned |
| FR-5.2      | Must     | PALETTE-002, PALETTE-004                              | Planned |
| FR-5.3      | Should   | PALETTE-001 (build fail proves enforcement)           | Planned |
| FR-6.1      | Must     | AA-001                                                | Planned |
| FR-6.2      | Must     | AA-002                                                | Planned |
| FR-6.3      | Should   | AA-003                                                | Planned |
| NFR-1.1     | Must     | SM-002 with cycle-count instrumentation (host or target) | Planned |
| NFR-1.2     | Should   | CPU profiler measurement during AT-1                  | Planned |
| NFR-2.1     | Must     | WAKE-001 / WAKE-004 with stopwatch / oscilloscope     | Planned |
| NFR-2.2     | Should   | Visual inspection during DIM-001/DIM-002              | Planned |
| NFR-3.1     | Must     | Microbenchmark during DEDUPE-007                      | Planned |
| NFR-3.2     | Should   | AT-3                                                  | Planned |
| NFR-4.1     | Must     | SM-071                                                | Planned |
| NFR-5.1     | Should   | Coverage report from SM-* host tests                  | Planned |
| NFR-6.1     | Should   | AT-2                                                  | Planned |

---

## 9. Troubleshooting Guide

| Symptom                                              | Likely Cause                                                      | Diagnostic Steps                                                              | Corrective Action |
|------------------------------------------------------|-------------------------------------------------------------------|-------------------------------------------------------------------------------|-------------------|
| Panel never dims                                     | Codex daemon pushing on every tick (dedupe regressed)             | Tail daemon at debug; expect dedupe drops in steady state.                    | Re-check `_last_pushed_snapshot` is updated only on `result.ok`. |
| Panel dims but never turns off                       | `EV_TIME` not firing at 1 Hz                                       | Add `ESP_LOGD` in `EV_TIME` handler; verify timer running.                    | Re-arm `esp_timer`; check timer queue depth. |
| Panel turns off and never comes back                 | Wake source unwired                                                | Manual motion test (WAKE-001) and touch test (WAKE-002).                      | Inspect adapter event queue and ISR registration. |
| UI rotates wildly                                    | Hysteresis band too narrow or noisy accel                          | Inspect raw accel samples; widen `motion_threshold_mg` and rotation hysteresis. | Tune Kconfig. |
| Daemon pushes every minute even when nothing changes | `USAGE_PERCENT_TOLERANCE` smaller than upstream jitter             | Compare deltas in `_last_snapshot` vs `_last_pushed_snapshot`.                | Increase tolerance; document the new value. |
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
| `USAGE_PERCENT_TOLERANCE` (client)  | 0.005 (= 0.5 percentage points in `[0,1]`) | This FSD |
| SM tick cadence                     | 1 Hz                          | This FSD |
| Palette: max channel cap            | 0xC0 per channel              | A4 source spec |
| Palette: saturated-blue rule        | B ≥ 0xB0 AND R < 0x40 AND G < 0x40 → reject | A4 source spec |

### 10.2 Example Idle Sequence (SM trace)

```
t=0       init                      → ACTIVE, br=70, panel=on, changed=1
t=60 s    EV_TIME                   → ACTIVE, br=70, panel=on, changed=0
t=5 min   EV_TIME                   → DIMMED, br=20, panel=on, changed=1
t=10 min  EV_TIME                   → DIMMED, br=20, panel=on, changed=0
t=30 min  EV_TIME                   → OFF,    br=0,  panel=off, changed=1
t=30:05   EV_PUSH                   → ACTIVE, br=70, panel=on, changed=1
t=30:06   EV_TIME                   → ACTIVE, br=70, panel=on, changed=0
```

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
- `[[burnscope/docs/codex-app-server.html]]` — `account/rateLimits/updated` cadence assumption (A-4).
- `[[burnscope/CLAUDE.md]]` — repository conventions.
- `[[burnscope/docs/ESP32-S3-AMOLED-1.43-Demo]]` - Waveshare demo code for ESP32-S3-AMOLED-1.43.
