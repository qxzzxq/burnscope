# OLED Burn-In Protection

OLEDs degrade per-subpixel based on cumulative emission. Static, bright,
blue-heavy regions wear out faster than the surrounding pixels, leaving a
permanent ghost ("burn-in"). For an always-on display like BurnScope —
where the same UI elements (labels, gauges, identifier text) sit in the
same coordinates for hours — mitigation is mandatory, not optional.

Below is a catalog of common techniques, grouped by category, with notes
on how each one trades off against the use case.

---

## 1. Spatial mitigations (move the pixels)

### 1.1 Pixel shifting (a.k.a. orbit / jitter)
Periodically translate the entire framebuffer by a few pixels along a
slow path (square, circle, or random walk). Each subpixel under a static
element sees the "on" state for a fraction of the time instead of
continuously.

- Period: typically 30 s – several minutes per step.
- Amplitude: 1–8 px. Larger is more effective but more visually
  noticeable.
- Cheap to implement: just an offset added at draw time.
- Caveat: requires a margin (or letterbox) around the active area so
  shifted content doesn't clip.

### 1.2 Element-level shifting / layout jitter
Instead of moving the whole frame, periodically nudge individual UI
elements (clock position, status icons) by 1–2 px. Less perceptible than
full-frame shift but only protects the moved elements.

### 1.3 Periodic layout rotation
Swap between two or more layouts that place the same information at
different coordinates (e.g. flip the gauge from left to right every
hour). Effective for long-running displays where shifting alone is
insufficient.

---

## 2. Temporal mitigations (turn the pixels off)

### 2.1 Screen off / deep sleep on idle
After N minutes of no state change (or no user interaction), blank the
panel entirely. Most effective mitigation by far — zero emission means
zero wear.

- Wake trigger: touch, button, motion sensor, or any new data push.
- For BurnScope: could blank when no agent activity in the last hour.

### 2.2 Dimming on idle
Step brightness down (e.g. 100 % → 30 %) after a short idle window
before the full screen-off. Useful as an intermediate state so the
display remains glanceable.

### 2.3 Screensaver / animated overlay
Replace the static UI with a moving pattern (clock face, bouncing logo,
particle field) after N minutes idle. Less aggressive than full
blanking; the moving content guarantees no single pixel is held on.

### 2.4 Duty-cycled refresh
For panels that support it, lower the refresh rate while idle. Doesn't
directly reduce burn-in but reduces overall power and heat (which
accelerates degradation).

---

## 3. Brightness / luminance mitigations

### 3.1 Global brightness cap
Run the panel below its maximum brightness at all times. Burn-in rate is
super-linear in luminance, so a 70 % cap can more than double panel
life vs. 100 %.

### 3.2 Automatic Brightness Limiter (ABL)
Reduce overall brightness when a large fraction of the screen is lit
(common in TV/phone OLEDs). For an embedded UI you can approximate this
by measuring the framebuffer's lit-pixel ratio and scaling brightness
accordingly.

### 3.3 Logo / static-region dimming
Detect regions of the framebuffer that haven't changed for some window
(say, > 30 s) and dim them by 20–40 %. Industry term: "Logo Luminance
Adjustment". Requires per-region change tracking.

### 3.4 Ambient-light-adaptive brightness
With an LDR or ambient sensor, drop brightness in dark rooms. Saves
wear and is more comfortable to look at.

---

## 4. Content-design mitigations (choose pixels carefully)

### 4.1 Dark mode / black background
OLED pixels at pure black are off — zero emission, zero wear. Prefer
dark backgrounds with light foreground over light backgrounds. This is
the single most impactful design choice.

### 4.2 Avoid pure white for static elements
White drives all three subpixels at full brightness simultaneously.
Replace large static white areas with light grey (e.g. #C0C0C0) or
tinted colors — perceptually similar, significantly less wear.

### 4.3 Avoid saturated blue for static elements
Blue subpixels (deep-blue OLED emitters) degrade fastest — often 2–4×
the rate of red/green. Static blue UI chrome (status bars, frames) ages
the panel asymmetrically and produces a yellow-tinted burn-in. Prefer
red, green, amber, or white-tinted hues for persistent elements.

### 4.4 Anti-aliasing / soft edges
Sharp 1-pixel edges (text, gauge boundaries) create visible wear lines
once burn-in starts. Slight anti-aliasing or 1-px feathering spreads
the wear across neighboring pixels and makes ghosting less perceptible.

### 4.5 Vary the content
Rotate displayed information on a slow cadence (e.g. swap between
"tokens used" and "tokens remaining" every few minutes). Anything that
keeps the bit pattern from being identical hour-to-hour helps.

---

## 5. Panel-driven / hardware mitigations

### 5.1 Pixel-refresh / compensation cycle
Many OLED controllers expose a "panel refresh" command that runs an
internal uniformity-compensation pass (often a slow full-screen sweep)
to equalize subpixel aging. Typically scheduled when the device goes
idle for a long period (e.g. nightly).

### 5.2 Demura / aging-compensation tables
Higher-end controllers track cumulative emission per region and apply
inverse correction in hardware. Generally not user-controllable on
hobbyist parts but worth checking the panel datasheet.

### 5.3 Temperature management
OLED degradation roughly doubles per 10 °C rise. Keeping the panel
cool (good ventilation, avoiding sun exposure, lowering MCU heat
coupling) extends life independently of any software mitigation.

---

## Recommended baseline for an always-on embedded meter

A minimal, effective set for a device like BurnScope:

1. **Dark background, no pure white, no static saturated blue.** (free,
   biggest single win)
2. **Global brightness cap** at ~60–70 % unless ambient is bright.
3. **Pixel-shift the whole frame** by 1–2 px on a 30–60 s cadence.
4. **Dim to ~20 %** after 5 minutes of no new data; **blank** after 30
   minutes.
5. **Wake on next push** (and optionally on touch).
6. **Nightly panel-refresh** if the driver exposes one.

Items 1–5 are entirely in firmware UI code; item 6 depends on the
SH8601/CO5300 command set.

---

## AMOLED build profile — concrete decisions

These are the settings chosen for the `amoled_sh8601` display profile
(Waveshare ESP32-S3-Touch-AMOLED-1.43, 466×466 round panel, onboard
QMI8658 6-axis IMU and capacitive touch).

### A1. Orientation-driven layout rotation (0° / 90° / 180° / 270°)
Read the QMI8658's **accelerometer** gravity vector — not the
gyroscope — to detect which edge of the round panel currently points
"down", and rotate the UI in 90° steps to match. Reference bring-up:
`ESP32-S3-AMOLED-1.43-Demo/ESP-IDF/03_I2C_QMI8658` (Waveshare).

Why accelerometer rather than gyroscope: the gyroscope reports angular
velocity and accumulates drift; the accelerometer reports a stable
gravity vector that maps directly to orientation quadrant. The QMI8658
exposes both — we only need the accel for this feature.

- **Sampling**: low-power accel ODR (e.g. `Qmi8658AccOdr_LowPower_21Hz`)
  is sufficient — orientation changes are slow.
- **Quadrant selection**: pick the axis (±X, ±Y) whose gravity
  component dominates; ignore changes smaller than a hysteresis band
  (e.g. require the new dominant axis to exceed the previous one by
  ~0.2 g) to avoid flapping at 45°.
- **Debounce**: only commit the rotation if the new orientation is
  stable for ~500 ms.
- **Burn-in benefit**: incidental — only helps when the user actually
  reorients the device. Primary motivation is UX (readable from any
  angle); the burn-in win is a bonus and does *not* substitute for
  pixel-shift or screen-off mitigations.

### A2. Idle dimming and screen-off, with multi-source wake

#### Firmware side: what counts as "activity"
Maintain a single `last_activity` timestamp updated by any of:

- accelerometer delta-magnitude exceeding a motion threshold
  (e.g. > 0.05 g change between consecutive low-rate samples),
- capacitive touch event,
- physical button press,
- **incoming `POST /summary` push** — but only because pushes are
  pre-filtered upstream to mean "something semantically changed"
  (see *Client side* below).

State machine driven off `now - last_activity`:

| Idle duration                | State        | Brightness   |
|------------------------------|--------------|--------------|
| `< IDLE_DIM_MINUTES`         | Active       | `DEFAULT_BRIGHTNESS` |
| `≥ IDLE_DIM_MINUTES`         | Dimmed       | `IDLE_BRIGHTNESS`    |
| `≥ IDLE_OFF_MINUTES`         | Display off  | panel sleep / 0      |

Defaults (Kconfig options under `BurnScope display → AMOLED burn-in`):

- `IDLE_DIM_MINUTES`   = **5**
- `IDLE_BRIGHTNESS`    = **20 %**
- `IDLE_OFF_MINUTES`   = **30**
- Motion threshold     = **0.05 g** (per-sample delta magnitude)

Any wake source returns the state machine to *Active* and restores
`DEFAULT_BRIGHTNESS` immediately — no fade-up, instant response feels
better when the user picks the device up.

#### Client side: active poll + dedupe in the Codex daemon

The naïve assumption — that `account/rateLimits/updated`
notifications would flow steadily from the `codex app-server`
subprocess our daemon manages — turned out to be wrong. The
long-lived app-server is a passive observer: it reads its
rate-limit cache from disk at bootstrap and never sees the changes
that happen when separate `codex` CLI processes burn tokens
elsewhere on the same machine. Verified empirically — `lsof` on
the running app-server shows zero file watchers on
`~/.codex/state_*.sqlite` and no IPC channels to the other CLI
processes. So if we sat on notifications alone, the daemon would
push exactly once at bootstrap and then go silent for the lifetime
of the connection.

The codex CLI's on-disk cache *does* advance with real activity,
and `account/rateLimits/read` against the long-lived app-server
*does* re-read from that cache on each call (verified by holding
one app-server alive across 120 s of real CLI activity and
observing `usedPercent` advance between two `read` calls into the
same process). So the daemon **actively polls**, **anchors**, and
**dedupes**:

1. Every `POLL_INTERVAL_S` seconds (default: 60), call
   `account/rateLimits/read` against the daemon's own app-server.
2. Build an `AgentSnapshot` from the result.
3. **Anchor** the fresh snapshot via
   `_anchor_resets_at(fresh, _last_pushed_snapshot)`: for each
   session whose `used_pct` matches the corresponding last-pushed
   entry, rewrite its `resets_at` to the last-pushed value. Codex's
   backend reports `resetsAt` as roughly `now + remaining`, so the
   raw value drifts ~60 s per 60 s of wall clock at low usage —
   without anchoring the dedupe key would change every poll and the
   firmware would be woken out of burn-in idle every minute. The
   firmware-side synthesis (`effective_resets_at` in `snapshot.h`)
   compensates for the now-stale stored `resets_at` whenever the
   session is `rolling` and `used_pct ≤ 0.01`.
4. Compare the anchored snapshot against `_last_pushed_snapshot`
   via `AgentSnapshot.semantically_equal(other)`.
5. If equal → log at debug and skip. If different → enqueue for
   `_pusher_loop`.
6. On the first iteration (`_last_pushed_snapshot is None`) always
   push, so the firmware has data immediately.
7. Update `_last_pushed_snapshot` only after the push completes
   with at least one device accepting it. A transport failure or
   auth drop leaves the previous value untouched, so the next
   poll re-attempts.

**Semantic equality** — no tolerance needed:

- Compare `agent` and per-session
  `(type, used_pct, resets_at, rolling, window_duration_mins)`
  tuples, sorted by `type` so session order is not significant.
- Ignore `captured_at` (bumps every read, not user-visible state).
- The anchoring step above papers over codex's wall-clock-driven
  `resets_at` drift, so byte-exact comparison stays meaningful even
  though the raw wire value is unstable.
- `usedPercent` is a `f64` in the openai/codex source (the wire
  spec previously said "integer"; corrected in
  `docs/codex-app-server.html`). In practice the ChatGPT-plan
  backend ships integer-valued percentages so byte-exact still
  works, but consumers shouldn't lean on integer-exact equality.
- A `resets_at` change with `used_pct` *also* changing counts as a
  real transition (anchor doesn't fire). A `resets_at` change with
  `used_pct` unchanged is presumed to be wall-clock drift and gets
  anchored back to the last-pushed value.

Coexistence with the existing notification path: the daemon's
`_dispatch` handler for `rateLimits/updated` is kept as-is.
Costs nothing today and would be a free win if a future codex
release ever propagates notifications across processes.

**Claude statusline** already has this property naturally: the
statusline only fires when the user is active in Claude Code, so
each fire is implicit evidence of presence. No active polling
needed there for the idle-wake use case.

While the display is dimmed or off, the HTTP server keeps running.
Any push that *does* arrive (because the upstream gating decided it
was meaningful) both updates the framebuffer **and** wakes the panel
— which is exactly what we want: a real change in your usage is the
one signal that should pull your eye back to the device.

**Known limitation — multi-machine staleness.** A daemon installed
on PC A polls PC A's local cache only. Codex activity on PC B
won't update PC A's cache, so PC A's panel can drift behind the
true global account state. Accepted for v1; cross-machine sync is
out of scope.

#### Implementation notes
The idle state machine is implemented as a **pure-logic module**,
fully decoupled from hardware. This keeps it host-testable and
reusable across display profiles (e.g. the CYD board could pull it
in unchanged with its own adapter).

**Module layout**

```
firmware/main/burn_protection/
├── burn_idle.h          # public API
├── burn_idle.c          # pure logic, deps: <stdint.h>, <stdbool.h> only
└── test/                # host-runnable unit tests
firmware/main/displays/amoled_sh8601/
└── burn_idle_adapter.c  # wires IMU + touch + button + HTTP + panel
```

**Pure-module API (sketch)**

```c
typedef enum { BURN_IDLE_ACTIVE, BURN_IDLE_DIMMED, BURN_IDLE_OFF }
    burn_idle_state_t;

typedef enum {
    BURN_IDLE_EV_MOTION,    // accel delta above threshold
    BURN_IDLE_EV_TOUCH,
    BURN_IDLE_EV_BUTTON,
    BURN_IDLE_EV_PUSH,      // POST /summary received
    BURN_IDLE_EV_TIME,      // re-evaluate against current time
} burn_idle_event_t;

typedef struct {
    burn_idle_state_t state;
    uint8_t           brightness_pct;
    bool              panel_on;
    bool              changed;   // true iff output differs from previous step
} burn_idle_output_t;

burn_idle_output_t burn_idle_step(
    burn_idle_t *sm, burn_idle_event_t ev, int64_t now_us);
```

Note: the time-trigger event is named `EV_TIME`, not `EV_TICK` — the
SM has no opinion on cadence. See *Tick strategy* below.

**Brightness as config, not SM logic**

The SM owns *state transitions only*. The mapping from state to
brightness lives in a small config table outside the SM:

```c
typedef struct {
    int64_t  dim_after_us;
    int64_t  off_after_us;
    uint8_t  active_brightness_pct;   // default 70
    uint8_t  dimmed_brightness_pct;   // default 20
    int16_t  motion_threshold_mg;     // default 50 (= 0.05 g)
} burn_idle_config_t;
```

This means:

- "70 % default, 20 % dimmed" is config, not hardcoded.
- Brightness can be tuned (per profile, per build, per user setting)
  without touching SM logic or any SM tests.
- The SM's `brightness_pct` output is just a lookup of the current
  state into the config — trivially correct.

**Tick strategy: 1 Hz periodic, adapter-owned**

The adapter calls `burn_idle_step(sm, EV_TIME, now)` once per second
from a FreeRTOS timer. The SM compares `now - last_activity` against
`dim_after_us` / `off_after_us` and emits any state change.

Why periodic rather than deadline-based:

- Wi-Fi must stay up to receive pushes in idle, so the CPU never gets
  to deep-sleep regardless. The "no wasted ticks" win of a
  deadline-based scheduler is illusory on this hardware.
- The IMU adapter is already waking the CPU at ~21 Hz to sample
  accelerometer data — a 1 Hz SM tick disappears into that noise.
- Tests are simpler: every step is `(event, time) → output`, no
  assertions on "did the SM request the right next timer."
- Race-free: an event arriving between ticks just updates
  `last_activity`; the next tick re-evaluates against current time.

Because the SM exposes `EV_TIME` (not `EV_TICK`), the cadence
decision is the adapter's alone. If a future profile needs
deadline-based scheduling (e.g. a battery-powered variant where Wi-Fi
*can* sleep), we add a `burn_idle_next_deadline(sm)` accessor and
swap the adapter — SM tests don't change.

**Adapter responsibilities (the I/O boundary)**

| Source                          | Adapter action                          |
|---------------------------------|-----------------------------------------|
| QMI8658 accel samples (~21 Hz)  | Compute delta-g; if > threshold → `EV_MOTION` |
| Touch controller IRQ            | → `EV_TOUCH`                            |
| Button GPIO IRQ                 | → `EV_BUTTON`                           |
| `POST /summary` HTTP handler    | → `EV_PUSH`                             |
| 1 Hz `esp_timer`                | → `EV_TIME`                             |
| `output.brightness_pct` change  | Call panel driver brightness setter     |
| `output.panel_on` change        | Call panel sleep / wake                 |

The SM never includes `esp_lcd_*`, `driver/i2c_*`, `driver/gpio.h`,
or LVGL headers. Anything ESP-IDF-shaped lives strictly in the
adapter.

**Host testing**

`burn_idle.c` compiles cleanly with a host C compiler (no ESP-IDF
toolchain needed). Tests are scripted event sequences:

```c
TEST_CASE("dim after 5 min idle, off after 30, motion wakes") {
    burn_idle_t sm; burn_idle_init(&sm, default_config);

    step(&sm, EV_TIME, MIN(4) + SEC(59));
    assert_state(sm, ACTIVE);

    step(&sm, EV_TIME, MIN(5) + SEC(1));
    assert_state(sm, DIMMED);

    step(&sm, EV_TIME, MIN(30) + SEC(1));
    assert_state(sm, OFF);

    step(&sm, EV_MOTION, MIN(30) + SEC(5));
    assert_state(sm, ACTIVE);
    assert_brightness(sm, 70);
}
```

Add tests for: each wake source individually; PUSH-as-wake parity
with motion; threshold-just-below vs. threshold-just-above; rapid
event flapping; config edge cases (`dim_after_us == off_after_us`).

### A3. Default brightness: 70 %
Sets `DEFAULT_BRIGHTNESS = 70 %` of panel max. Matches the §3.1 cap
recommendation. Exposed as `BURNSCOPE_AMOLED_DEFAULT_BRIGHTNESS` in
Kconfig for downstream tuning.

### A4. Palette acceptance check: no pure white, no saturated blue
A CI / review check, not a runtime mitigation. Any color token used in
the AMOLED UI must satisfy:

- **No pure white**: max channel-sum ≤ `0xC0 + 0xC0 + 0xC0` (i.e. cap
  at `#C0C0C0`-equivalent luminance for any static element).
- **No saturated blue**: for any color where the blue channel is the
  dominant component, require `B ≤ 0xB0` *and* either R or G ≥ `0x40`
  (i.e. no `#0000FF`-class pure-blue chrome).

Codify the rule as a small token-validator in the firmware build (or
as a review checklist item in `docs/ui/`).

### A5. Soft edges (1-px feathering)
All static UI boundaries — text glyphs, gauge arcs, dividers, frame
borders — must be rendered with ≥ 1-px anti-aliasing. Sharp 1-pixel
edges concentrate wear on a single row/column of subpixels and produce
the most visible burn-in lines.

- LVGL: enable anti-aliased label rendering (`LV_FONT_SUBPX_*` /
  `lv_obj_set_style_anti_aliasing`) and use `LV_DRAW_SW_COMPLEX = 1`
  so arcs and rounded rects feather correctly.
- Hand-drawn primitives (gauges, custom widgets) must blend the edge
  pixel at ~50 % intensity rather than a hard on/off boundary.

### Open questions
- **Pixel-shift interaction with rotation**: when A1 rotates the
  layout, the pixel-shift orbit (§1.1) should reset its phase so the
  cumulative offset doesn't compound across rotations. Decide whether
  shift state is per-orientation or global.
- **Touch as wake while display is off**: confirm the AMOLED-1.43
  touch controller (`CST816`-class) can interrupt the host from a
  low-power state without a full panel re-init.
