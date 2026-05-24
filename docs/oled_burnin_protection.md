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
