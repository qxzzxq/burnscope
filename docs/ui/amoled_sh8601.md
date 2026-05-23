# Design spec — `amoled_sh8601` profile

**Target board.** Waveshare ESP32-S3-Touch-AMOLED-1.43
([wiki](https://www.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.43)).
466×466 round AMOLED, SH8601 *or* CO5300 over QSPI (Waveshare
dual-sources the panel; both speak the same protocol), CST820 over I2C
(touch **unused** in MVP), ESP32-S3 with 8 MB PSRAM + 8 MB flash.
LVGL 9.x — same as the existing cyd2usb profile.

**Design concept.** Concentric-arc dial with a stacked core. Each
session row becomes one ring around a central core that carries the
agent identity, the per-row session type chip, the percentage, and the
reset countdown. Two agents (claude, codex) cycle via the existing
1 Hz tick (`CYCLE_INTERVAL_S = 5`).

This spec is the contract between design and firmware. Pixel values
here are the layout currently locked in `displays/amoled_sh8601/ui.c`,
which is in turn pasted from the HTML tuner at
`firmware/scripts/amoled-preview.html`. The HTML is the source of truth
for visual changes; the constants here are kept in sync by hand.

---

## 1. Layout — agent screen

Origin at the centre `(cx, cy) = (233, 233)`. Angles use the LVGL arc
convention (0° = 3 o'clock, clockwise positive).

### 1.1 Safe disc

- Visible disc radius = **233 px** (hardware mask).
- **Safe radius = 220 px.** 13 px rim margin covers panel-edge dimming,
  mask anti-alias tolerance, and the step LVGL leaves at arc end caps.
- No glyph centroid past r = 200; no arc centreline past r = 215.

### 1.2 Two rings (outer → inner)

Two rings match the two sessions both Claude (`current`, `weekly`) and
Codex (`primary`, `secondary`) currently emit. Ring index = palette
index = session index, so row 0 is the *outermost* arc — the heaviest
visual weight goes to the headline session.

| Ring | Session | Outer r | Inner r | Stroke | Claude     | Codex      |
|------|---------|---------|---------|--------|------------|------------|
| R0   | 0       | 215     | 197     | 18     | `0xDE7356` | `0x81C3DD` |
| R1   | 1       | 192     | 178     | 14     | `0xA4A049` | `0xA4A049` |

- 5 px inter-ring gap (197→192).
- Stroke taper signals emphasis hierarchy — M3 *emphasis through
  weight*, not saturation.
- Track (unfilled portion) = `#2F2F2F`, `LV_OPA_COVER`.
- Indicator (filled portion) = per-agent palette hex from the table.
- **Arc sweep:** start 135°, end 45° clockwise = **270° total**,
  symmetric about the vertical axis with a 90° opening at the top.
- `used_pct = 0` → no indicator visible. `used_pct = 1.0` → full 270°.
- Rounded end caps on the indicator; square ends on the track.

Snapshots from agents that emit a third session are accepted by the
firmware (`SNAPSHOT_MAX_SESSIONS = 3` is preserved) but the AMOLED
profile renders only the first two rows.

### 1.3 Central core

The core is a vertical stack centred on `cx`:

| Element                    | Vertical centre | Font / Asset                | Colour     |
|----------------------------|-----------------|-----------------------------|------------|
| Brand icon (70×70 ARGB)    | y = 130         | `icon_<agent>_70`           | native     |
| Row-0 type chip            | y = 195         | Montserrat 16               | `#F9F2DF` on `#2E2E2E` |
| Row-0 percentage           | y = 235         | Montserrat 48               | `#F9F2DF` |
| Row-0 countdown            | y = 235 (bottom-aligned with %, to its right) | Montserrat 14 | `#5C5C5C` |
| Row-1 type chip            | y = 285         | Montserrat 16               | `#F9F2DF` on `#2E2E2E` |
| Row-1 percentage           | y = 315         | Montserrat 36               | `#F9F2DF` |
| Row-1 countdown            | y = 315 (bottom-aligned with %, to its right) | Montserrat 14 | `#5C5C5C` |

- **Chip styling.** Rounded background, radius 4, padding (5h, 3v).
  Chip text = `session.type` rendered verbatim from the wire (e.g.
  `"current"`, `"weekly"`, `"primary"`).
- **% + countdown rows.** Each row is a `LV_SIZE_CONTENT` flex
  container with `cross_align = END` so the countdown's baseline sits
  flush with the percentage's bottom edge. Re-centre the row each
  render to keep the optical centre stable as the percentage glyph
  count changes ("1%" → "100%").
- **Countdown source.** `format_countdown(resets_at - now)` —
  `Xd XXh` / `Xh XXm` / `Xm XXs`, `"reset due"` when negative,
  `"syncing..."` while the wall clock is unsynced.

The AMOLED's true black is the core surface — no fill, no stroke. M3
elevation via *absence* rather than tint, justified by the AMOLED
power/burn-in profile.

### 1.4 Footer

Two stacked centred lines at the bottom of the safe disc:

| Line   | Vertical centre | Content                                       |
|--------|-----------------|-----------------------------------------------|
| Top    | y = 410         | `format_updated_relative(captured_at)` ("updated 3 min ago") |
| Bottom | y = 430         | `client_id` from NVS, or `"unpaired"` if empty |

Both Montserrat 14, colour `#5C5C5C`, full-disc width, ellipsis clamp
(`LV_LABEL_LONG_DOT`) on the bottom line.

---

## 2. Splash screen

Centred in the disc, no rim usage.

| Element            | Position    | Font / Source              | Colour     |
|--------------------|-------------|----------------------------|------------|
| "BurnScope" title  | (233, 180)  | Montserrat 28              | `#F9F2DF` |
| Status line        | (233, 233)  | Montserrat 24, max width 360 px, wrap | `#F9F2DF` |
| `v<BURNSCOPE_FW_VERSION>` | (233, 410) | Montserrat 14         | `#808080` |

Background `#000000`. No splash logo — the agent icons only appear on
the data screen. The version label shares the agent-screen footer
band so the three-line provisioning splash ("Setup mode / Join … /
Open 192.168.4.1") never collides with it.

---

## 3. Type scale

| M3 slot         | Use                                     | Font          | Status                  |
|-----------------|-----------------------------------------|---------------|-------------------------|
| Display Small   | Row-0 percentage                        | Montserrat 48 | Bundled, enabled in `sdkconfig.defaults.esp32s3`. |
| Display Smaller | Row-1 percentage                        | Montserrat 36 | Bundled, enabled in `sdkconfig.defaults.esp32s3`. |
| Headline Small  | Splash title                            | Montserrat 28 | Existing.               |
| Title Medium    | Splash status                           | Montserrat 24 | Existing.               |
| Body Medium     | Row-0 / row-1 type chips                | Montserrat 16 | Existing.               |
| Label Medium    | Footer + per-row countdowns + splash version | Montserrat 14 | Enabled in `sdkconfig.defaults.esp32s3`. |

The HTML preview asks for Montserrat 15 / 35 / 60 to maximise visual
hierarchy. LVGL bundles only even sizes in the 10–48 range, so the
firmware uses the nearest standard cuts (M16 / M36 / M48) and skips
the custom-font-generation step. Visual delta is ≤ 1 px per glyph and
not perceptible at panel-viewing distance.

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
    .            v0.4.1           .   ← y=410, M14, #808080
     .                           .
      .                         .
        .                     .
          .                 .
              ` -  -  - `
```

### Agent screen (claude, row 0 = 73%, row 1 = 40%)

```
            ╭─────────────────╮
        ╱── outer R0 ──────────── ╲
      ╱  ╭─── inner R1 ──────╮   ╲
     │   │                    │    │
     │   │     ┌─────────┐    │    │   ← icon 70×70 at (233, 130)
     │   │     │  icon   │    │    │
     │   │     └─────────┘    │    │
     │   │      [current]     │    │   ← chip, M16, y=195
     │   │      73%   5h      │    │   ← M48 + M14 countdown, y=235
     │   │      [weekly]      │    │   ← chip, M16, y=285
     │   │      40%   7d      │    │   ← M36 + M14 countdown, y=315
     │   │                    │    │
     │   │  updated 3 min ago │    │   ← footer top, M14, y=410
     │   │  user@host         │    │   ← footer bottom, M14, y=430
     │   ╰────────────────────╯    │
      ╲                           ╱
        ╲    arcs 135°→45°       ╱
          ╲   (clockwise)      ╱
            ╲─────────────────╯
```

Ring fill (claude, clockwise from 135°):

- R0 fills **73% of 270°** = 197° → ends near 11 o'clock.
- R1 fills 40% of 270° = 108° → ends near 9 o'clock.

---

## 5. States

| State                                | Behaviour |
|--------------------------------------|-----------|
| Only one agent stored                | No cycling (existing `snapshot_store_count() < 2` rule). |
| `session_count == 1`                 | R0 + row-0 stack only. R1 arc + row-1 chip/%/countdown hidden via `LV_OBJ_FLAG_HIDDEN`. |
| `session_count == 2`                 | R0 + R1, both core stacks visible. |
| `session_count >= 3`                 | Extra slot silently ignored on AMOLED (CYD still renders three rows). |
| Unused rings                         | **Hidden entirely**, not greyed. Greying would imply "empty data" rather than "no such session" — mirrors cyd2usb's row-hide pattern. |
| Clock unsynced (`now < 1700000000`)  | Per-row countdowns read `"syncing..."`; percentages still render; footer top line empty. |
| Post-reset auto-zero (`now >= resets_at`) | Clamp `pct = 0` until next push. Ring collapses to zero, % reads `0%`, countdown reads `"reset due"`. |
| "unpaired" footer (empty NVS)        | Top line empty, bottom line `"unpaired"`. |
| Splash → agent first push            | Only swap on `s_visible_agent[0] == '\0'`; subsequent pushes re-render in place (1 Hz tick handles cycling). |

---

## 6. Tunable parameters (locked via `firmware/scripts/amoled-preview.html`)

The HTML tuner is the contract between this spec and the
firmware-engineer. Every value below is a live slider; the page emits
a JSON blob + a copy-paste C `#define` block for `ui.c`.

### Geometry — rings

- `cx`, `cy` — centre (default 233, 233).
- `safe_radius` — 220 (range 200–230).
- `ring0_outer_r`, `ring0_inner_r` — 215 / 197.
- `ring1_outer_r`, `ring1_inner_r` — 192 / 178.
- `arc_start_deg` — 135 (range 90–180).
- `arc_sweep_deg` — 270 (range 180–330).
- `arc_rounded_ends` — bool.

### Core

- `core_icon_y` — 130 (centre).
- `core_chip_primary_y` — 195.
- `core_primary_y` — 235.
- `core_chip_secondary_y` — 285.
- `core_secondary_y` — 315.
- `core_chip_radius` — 4.
- `core_chip_pad_h`, `core_chip_pad_v` — 5, 3.

### Footer

- `footer_updated_y` — 410.
- `footer_email_y` — 430.

### Colours (locked — shown as swatches, not editable)

Per [§1.2](#12-two-rings-outer--inner) and §1.3–1.4. Hex codes mirror
the cyd2usb profile so a multi-screen setup looks visually unified.

### Preview controls

- Agent toggle (claude / codex) — swaps icon and ring 0 colour.
- Session-count toggle (1 / 2) — hides R1 + row-1 stack.
- `used_pct` sliders for R0, R1.
- State toggles: clock unsynced, footer unpaired.
- Mask overlay: 233 px disc edge + 220 px safe-radius guide.

---

## 7. Open trade-offs

1. **70×70 brand icons regenerated.** SVG sources for both agents live
   at `firmware/scripts/icons/icon_{claude,codex}.svg` (extracted from
   the inline markup in `amoled-preview.html`). Run `rsvg-convert -w
   70 -h 70 -f png … | LVGLImage.py --cf ARGB8888 --ofmt C` to
   regenerate. Each PNG-derived asset is ≈19.6 KB raw → ≈40 KB total
   in the AMOLED build. Compiled in conditionally via
   `displays/amoled_sh8601/sources.cmake`; CYD's 24×24 assets in
   `main/icons/icon_{claude,codex}.c` are untouched.
2. **Font precision.** Picked nearest standard LVGL sizes (M14, M16,
   M36, M48) over generating custom Montserrat 14/15/35/60. Custom
   sizes would shave glyph-cap ambiguity but cost an extra build
   dependency (`lv_font_conv` Node.js tool) for a sub-pixel improvement.
   Revisit if the type hierarchy reads weak on the panel.
3. **AMOLED burn-in.** The dark surfaces + thin ring strokes already
   keep average pixel intensity low. A ±1 px layout drift every minute
   is a known mitigation; out of scope for MVP. Surface as a follow-up
   if persistent-display tests show evidence of burn.
