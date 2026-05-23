# Design spec — `amoled_co5300` profile

**Target board.** Waveshare ESP32-S3-Touch-AMOLED-1.43
([wiki](https://www.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.43)).
466×466 round AMOLED, CO5300 over QSPI, CST820 over I2C (touch
**unused** in MVP), ESP32-S3 with 8 MB PSRAM + 8 MB flash. LVGL 9.x —
same as the existing cyd2usb profile.

**Design concept.** Concentric-arc dial. Each session row becomes one
ring around a central core that carries the agent identity and the
headline percentage. Two agents (claude, codex) cycle via the existing
1 Hz tick (`CYCLE_INTERVAL_S = 5`).

This spec is the contract between design and firmware. Pixel values
here are the *starting* defaults — they are expected to be locked in
via `firmware/scripts/amoled-preview.html` before the constants are
copied into `displays/amoled_co5300/ui.c`.

---

## 1. Layout — agent screen

Origin at the centre `(cx, cy) = (233, 233)`. Angles use the LVGL arc
convention (0° = 3 o'clock, clockwise positive).

### 1.1 Safe disc

- Visible disc radius = **233 px** (hardware mask).
- **Safe radius = 220 px.** 13 px rim margin covers panel-edge dimming,
  mask anti-alias tolerance, and the step LVGL leaves at arc end caps.
- No glyph centroid past r = 200; no arc centreline past r = 215.

### 1.2 Three rings (outer → inner)

Three rings matches `SNAPSHOT_MAX_SESSIONS = 3`. Ring index = palette
index = session index, so row 0 is the *outermost* arc — the heaviest
visual weight goes to the headline session, matching how the cyd2usb
profile renders row 0 first.

| Ring | Session | Outer r | Inner r | Stroke | Claude     | Codex      |
|------|---------|---------|---------|--------|------------|------------|
| R0   | 0       | 215     | 197     | 18     | `0xDE7356` | `0x81C3DD` |
| R1   | 1       | 192     | 178     | 14     | `0xA4A049` | `0xA4A049` |
| R2   | 2       | 173     | 161     | 12     | `0xB0BEC5` | `0xB0BEC5` |

- 5 px inter-ring gaps (197→192, 178→173).
- Stroke taper signals emphasis hierarchy — M3 *emphasis through
  weight*, not saturation.
- Track (unfilled portion) = `#2F2F2F`, `LV_OPA_COVER`.
- Indicator (filled portion) = per-agent palette hex from the table.
- **Arc sweep:** start 135°, end 45° clockwise = **270° total**,
  symmetric about the vertical axis with a 90° opening at the top.
- `used_pct = 0` → no indicator visible. `used_pct = 1.0` → full 270°.
- Rounded end caps on the indicator; square ends on the track.

### 1.3 Central core (r ≤ 90)

| Element                      | Position    | Font / Asset         | Colour     |
|------------------------------|-------------|----------------------|------------|
| Brand icon (48×48 ARGB8888)  | (233, 188)  | `icon_<agent>_48`    | native     |
| Primary % (row 0)            | (233, 248)  | Montserrat 48        | `0xF9F2DF` |
| Type tag (row 0, e.g. "tokens") | (233, 282) | Montserrat 16        | `0xB0ACA0` |

The AMOLED's true black is the core surface — no fill, no stroke. M3
elevation via *absence* rather than tint, justified by the AMOLED
power/burn-in profile.

### 1.4 Header arc — "Usage"

- Flat `lv_label` (LVGL 9 has no curved-text widget; the 90° top gap is
  wide enough that flat reads cleanly).
- Position: (233, 14) centred horizontally.
- Font: Montserrat 28, colour `#F9F2DF`.
- Static string "Usage" — agent identity is communicated by the core
  icon and ring palette swap, not by the header.

### 1.5 Footer

Two flat labels at y = 410 (distance from centre = 177 < safe radius
220). Sits inside the innermost ring, below the core secondary label.

| Side  | Position    | Source                                     |
|-------|-------------|--------------------------------------------|
| Left  | x = 48      | `client_id` from NVS (`LV_LABEL_LONG_DOT`, max width 180 px) |
| Right | x = 418 right-aligned | `format_updated_relative(captured_at)` |

Both Montserrat 14, colour `#5C5C5C`. "unpaired" placeholder when the
NVS slot is empty (mirrors `render_footer_locked`).

### 1.6 Per-session ancillary text (R1, R2)

Row 0's tag and percentage live in the core. R1 and R2 need their own
type tag + countdown — placed as chip pills in the **top gap** at
y = 36, fixed angle so they never collide with the indicator at any
percentage:

| Ring | Pill centre | Pill (chip)                            | Countdown                       |
|------|-------------|----------------------------------------|---------------------------------|
| R1   | (170, 36)   | Montserrat 16 `#F9F2DF` on `#2E2E2E`, r=6, pad 8h/2v | Montserrat 14 `#B0ACA0` at (230, 36) |
| R2   | (296, 36)   | same                                   | Montserrat 14 `#B0ACA0` at (356, 36) |

If the chosen header font crowds the pills, the tuner will catch it —
either bump the header down to Montserrat 24 or move the pills below
the header band.

---

## 2. Splash screen

Centred in the disc, no rim usage.

| Element            | Position    | Font / Source              | Colour     |
|--------------------|-------------|----------------------------|------------|
| "BurnScope" title  | (233, 180)  | Montserrat 28              | `#F9F2DF` |
| Status line        | (233, 233)  | Montserrat 24, max width 360 px, wrap | `#F9F2DF` |
| `v<BURNSCOPE_FW_VERSION>` | (233, 410) | Montserrat 14         | `#808080` |

Background `#000000`. No splash logo — the agent icons only appear on
the data screen.

---

## 3. Type scale

| M3 slot         | Use                                     | Font          | Status                          |
|-----------------|-----------------------------------------|---------------|---------------------------------|
| Display Small   | Core primary readout (row 0 %)          | Montserrat 48 | **NEW.** Range-restricted to digits + `%` (~8 KB flash). |
| Headline Small  | "Usage" header                          | Montserrat 28 | Existing.                       |
| Title Medium    | Splash status                           | Montserrat 24 | Existing.                       |
| Body Medium     | Row 0 type tag, R1/R2 pill labels       | Montserrat 16 | Existing.                       |
| Label Medium    | R1/R2 countdowns, footer, splash version | Montserrat 14 | **NEW.** ASCII printable (~10 KB flash). |

Range-restrict M48 via LVGL's `--range` filter (`0x30-0x39,0x25`); M14
stays full ASCII so it can render any client_id.

**Fallback if the build budget is tight:** drop M14, use existing M16
for the footer/countdowns. Visual weight will be slightly heavier; not
a blocker.

---

## 4. Mockup

### Splash

```
              .  -  -  .
          .                 .
        .                     .
      .                         .
     .                           .
    .                             .
    .         BurnScope           .   ← y=180, M28, #F9F2DF
    .                             .
    .    Waiting for daemon...    .   ← y=233, M24, #F9F2DF
    .                             .
    .            v0.4.1           .   ← y=300, M14, #808080
     .                           .
      .                         .
        .                     .
          .                 .
              ` -  -  - `
```

### Agent screen (claude, row 0 = 73%, row 1 = 12%, row 2 = 4%)

```
                  Usage                       ← y=14, M28
            ╭─────────────╮
        [tokens 4h 12m]   [tokens 2d 03h]     ← y=36, R1 + R2 pills
       ╱                                ╲
     ╱  ┌─R0 outer 215 ──────────────┐    ╲
    │   │ ╭─R1 outer 192 ───────╮    │     │
    │   │ │ ╭─R2 outer 173 ───╮ │    │     │
    │   │ │ │                 │ │    │     │
    │   │ │ │    [icon 48]    │ │    │     │  ← icon centred (233,188)
    │   │ │ │                 │ │    │     │
    │   │ │ │      73%        │ │    │     │  ← M48, (233,248)
    │   │ │ │     tokens      │ │    │     │  ← M16, (233,282)
    │   │ │ │                 │ │    │     │
    │   │ │ │ user@host  3m   │ │    │     │  ← footer M14, y=410
    │   │ │ ╰─────────────────╯ │    │     │
    │   │ ╰─────────────────────╯    │     │
    │   ╰────arcs sweep 135°→45°─────╯     │
     ╲           (clockwise)              ╱
       ╲                                 ╱
         ╲─────────────────────────────╱
```

Ring fill (claude, clockwise from 135°):

- R0 fills **73% of 270°** = 197° → ends near 11 o'clock.
- R1 fills 12% of 270° ≈ 32° → ends near 8 o'clock.
- R2 fills 4% of 270° ≈ 11° → ends just past 7:30.

---

## 5. States

| State                                | Behaviour |
|--------------------------------------|-----------|
| Only one agent stored                | No cycling (existing `snapshot_store_count() < 2` rule). |
| `session_count == 1`                 | R0 + core only. R1/R2 arcs and pills hidden via `LV_OBJ_FLAG_HIDDEN`. |
| `session_count == 2`                 | R0 + R1. R2 hidden. R1 pill remains in the top gap. |
| `session_count == 3`                 | All three rings, both top-gap pills shown. |
| Unused rings                         | **Hidden entirely**, not greyed. Greying would imply "empty data" rather than "no such session" — mirrors cyd2usb's row-hide pattern. |
| Clock unsynced (`now < 1700000000`)  | Countdowns read "syncing…"; core % still renders (no clock dependency); footer right empty. |
| Post-reset auto-zero (`now >= resets_at`) | Clamp `pct = 0` until next push. Ring collapses to zero, % reads `0%`, countdown reads "reset due". |
| "unpaired" footer (empty NVS)        | Left = "unpaired", right = "". |
| Splash → agent first push            | Only swap on `s_visible_agent[0] == '\0'`; subsequent pushes re-render in place (1 Hz tick handles cycling). |

---

## 6. Tunable parameters (locked via `firmware/scripts/amoled-preview.html`)

The tuner is the contract between this spec and the firmware-engineer.
Every value below becomes a live slider; the HTML emits a JSON blob +
a copy-paste C `#define` block.

### Geometry — rings

- `cx`, `cy` — centre (default 233, 233).
- `safe_radius` — 220 (range 200–230).
- `ring0_outer_r`, `ring0_inner_r` — 215 / 197.
- `ring1_outer_r`, `ring1_inner_r` — 192 / 178.
- `ring2_outer_r`, `ring2_inner_r` — 173 / 161.
- `arc_start_deg` — 135 (range 90–180).
- `arc_sweep_deg` — 270 (range 180–330).
- `arc_rounded_ends` — bool.

### Core

- `core_icon_size` — 48 (range 32–64).
- `core_icon_y` — 188.
- `core_primary_y` — 248.
- `core_primary_font_size` — 48 (preview only; firmware build cost real).
- `core_secondary_y` — 282.

### Header

- `header_y` — 14.
- `header_font_size` — 28.

### Footer

- `footer_y` — 410.
- `footer_left_x`, `footer_right_x` — 48, 418.
- `footer_font_size` — 14.
- `footer_left_width` — 180 (ellipsis clamp).

### Pills (R1, R2)

- `pill_y` — 36.
- `pill_r1_x`, `pill_r2_x` — 170, 296.
- `pill_font_size` — 16.
- `pill_chip_radius` — 6.
- `pill_chip_padding_h`, `pill_chip_padding_v` — 8, 2.
- `pill_countdown_offset_x` — 60.

### Colours (locked — shown as swatches, not editable)

Per [§1.2](#12-three-rings-outer--inner) and §1.3–1.6. The hex codes
are inherited verbatim from `displays/cyd2usb_st7789/ui.c:113–116` and
the surrounding constants — the rule is "keep the current colour code"
across profiles.

### Preview controls

- Agent toggle (claude / codex) — swaps icon and ring 0 colour.
- Session-count toggle (1 / 2 / 3) — hides unused rings + pills.
- `used_pct` sliders for R0, R1, R2.
- State toggles: clock unsynced, post-reset zero, unpaired footer.
- Mask overlay: 233 px disc edge + 220 px safe-radius guide.

---

## 7. Open trade-offs

1. **48×48 brand icons.** Existing assets are 24×24 (`icons/icon_*.c`).
   At 48 px in the core they will be regenerated from the same lobehub
   SVG sources via the existing `rsvg-convert` + `LVGLImage.py`
   pipeline. If the upscale aliases badly, fallback is to render a
   single-letter "C" / "X" in Montserrat 56 in the core slot and drop
   the icon dependency. Decide at the icon-regen step.
2. **Curved header text.** Flat `lv_label` is the chosen compromise.
   Pre-rasterising "Usage" as an arc-aligned image would cost ~3 KB
   per character and is overkill for a fixed string. If the header
   feels visually anaemic at the chosen geometry, the next move is to
   drop the word "Usage" entirely and let the core's primary % +
   icon speak for themselves.
3. **AMOLED burn-in.** The dark surfaces + thin ring strokes already
   keep average pixel intensity low. A ±1 px layout drift every minute
   is a known mitigation; out of scope for MVP. Surface as a follow-up
   if persistent-display tests show evidence of burn.
