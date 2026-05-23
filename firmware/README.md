# BurnScope Firmware

ESP-IDF firmware for two display boards:

- **Cheap Yellow Display** (CYD, `cyd2usb` variant, ESP32, ST7789 320×240) —
  the original MVP target.
- **Waveshare ESP32-S3-Touch-AMOLED-1.43** (round 466×466 AMOLED driven
  by the dual-sourced SH8601/CO5300 controller over QSPI).

The two profiles ship in the same image — pick one at build time via
Kconfig (see *Display profiles* below). Phase 2 — captive-portal
provisioning + snapshot rendering — corresponds to FSD § 3.2.

## Prerequisites

- ESP-IDF v6.0.1 at `~/.espressif/v6.0.1/esp-idf` (run its
  `export.sh`/`export.fish` to put `idf.py` on `$PATH`).
- A 2.4 GHz WPA2/WPA3-PSK network the device can reach.

## Build, flash, monitor

```sh
idf.py set-target esp32        # CYD (cyd2usb) — default
# or
idf.py set-target esp32s3      # Waveshare AMOLED-1.43

idf.py build
idf.py -p <PORT> flash monitor
```

The target chip drives the default display profile via the
`sdkconfig.defaults.<target>` overlay (esp32 → cyd2usb_st7789;
esp32s3 → amoled_sh8601). Switch profiles within a target via
`idf.py menuconfig` → *BurnScope display*.

No WiFi credentials are baked into the image — the device captures them
over a captive portal on first boot.

## First-time provisioning

On a fresh (or freshly-reset) device:

1. The panel shows `Setup mode — connect to BURNSCOPE-XXXX`.
2. From a phone/laptop, join the open WiFi network
   **`BURNSCOPE-XXXX`** where `XXXX` is the last four hex digits of the
   board's MAC.
3. The OS captive-portal popup opens the form automatically (Apple/
   Android probes are intercepted). If not, browse to
   <http://192.168.4.1/>.
4. Pick your network from the scanned list, enter the password, hit
   **Save & reboot**.
5. The device persists the credentials to NVS and restarts. Subsequent
   boots skip the portal and join STA directly.

The panel walks through:

```
Booting…  ──►  Connecting…  ──►  Waiting for daemon…
```

…and switches to the agent screen the moment the first
`POST /summary` arrives.

## Re-provisioning

Two equivalent paths:

**Long-press the BOOT button** (GPIO0) for ≥ 5 s. The device wipes both
the WiFi credentials and the per-agent pairing slots, then reboots
into the captive portal. The next `POST /summary` from any laptop will
rebind verbatim (TOFU).

A network-triggered factory-reset endpoint isn't exposed in MVP — the
route had no auth and was pulled until an auth scheme lands. Use the
BOOT button in the meantime.

## Display profiles (build parameter)

The panel driver and the UI layout ship together as a build-time profile
under `main/displays/<name>/`. The active profile is chosen via Kconfig
(`menuconfig` → *BurnScope display* → *Display profile*) and pinned in
`sdkconfig.defaults` (or the target-specific overlay). Profiles shipped:

| Kconfig symbol                            | Target chip | Profile path                       |
|-------------------------------------------|-------------|------------------------------------|
| `CONFIG_BURNSCOPE_DISPLAY_CYD2USB_ST7789` | esp32       | `main/displays/cyd2usb_st7789/`    |
| `CONFIG_BURNSCOPE_DISPLAY_AMOLED_SH8601`  | esp32s3     | `main/displays/amoled_sh8601/`     |

The AMOLED profile targets the Waveshare ESP32-S3-Touch-AMOLED-1.43
(466×466 round AMOLED via QSPI; no touch wired in MVP). The board
ships with either an SH8601 or a CO5300 driver IC — same QSPI
protocol — and we link against Espressif's `esp_lcd_sh8601` managed
component, hence the profile name. It shares the wire format and
snapshot store with the CYD profile; only the rendering changes
(concentric arcs vs. linear bars).

The UI layout for the AMOLED profile is dialled in via the
configurator at `firmware/scripts/amoled-preview.html` (open in any
browser). It mirrors the role of `font-preview.html` for the CYD
profile: live sliders for ring radii / arc angles / core geometry /
header / footer / pills, with a copy-paste `#define` block at the
bottom that gets pasted into `main/displays/amoled_sh8601/ui.c`. The
full design spec is at `docs/ui/amoled_sh8601.md`.

Adding a new screen (e.g. an OLED or e-paper variant) is a
directory-drop operation. In `main/displays/<name>/`, create four files:

- `driver.c` — panel/SPI bring-up + LVGL port setup, exposing a
  profile-private init helper to `ui.c`.
- `ui.c` — LVGL layout implementing
  `display_profile_init`/`show_status`/`show_agent`.
- `Kconfig` — one line: `config BURNSCOPE_DISPLAY_<NAME>` declaring the
  `bool` prompt (it is rsource'd into the enclosing `choice`).
- `sources.cmake` — `if(CONFIG_BURNSCOPE_DISPLAY_<NAME>)` guarded
  `list(APPEND srcs ...)` + `list(APPEND inc ...)` for this profile's
  files; auto-discovered by the top-level `file(GLOB)` loop.

Then append one `rsource "displays/<name>/Kconfig"` line to the
`BURNSCOPE_DISPLAY` choice in `main/Kconfig.projbuild` (Kconfig has no
glob, so this is the only edit outside the profile directory). Run
`idf.py reconfigure` (or `menuconfig`) to pick up the new fragment.

UI layout belongs in the profile because layout choices are
geometry-bound (FR-5 in the FSD).

## Verifying Phase 2

The on-device smoke test:

```sh
HOST=burnscope-XXXX.local        # or the IP if mDNS is blocked
./scripts/phase2_smoke.sh "$HOST"
```

The script covers TC-SUM-100/101/102/103/104/105 and TC-HEALTH-100.
Visual checks remain a manual pass against FSD § 6.1.6 (two rounded
rows, integer percentage, "resets in HH:MM" countdown ticking once a
second).

| Test       | What it checks                                                           |
|------------|---------------------------------------------------------------------------|
| TC-CP-100  | Erase NVS, boot, walk through portal → STA-connected without re-flash.   |
| TC-NVS-102 | BOOT button held ≥ 5 s ⇒ portal returns on the next boot.                |
| TC-SUM-100 | `POST /summary` with `docs/examples/summary-push.json` → 204, UI repaints.|
| TC-SUM-101 | Push claude, then codex — UI cycles between them every ~5 s.             |
| TC-SUM-104 | `used_pct = 1.5` ⇒ 400.                                                  |
| TC-SUM-105 | 20 KiB body ⇒ 413.                                                       |
| TC-UI-101  | Push `resets_at = now() + 3600`, countdown decrements once per second.   |

## Out of scope for Phase 2

Phase-3 hardening — 24 h soak (AT-1), AP-mode fall-back after N=5 STA
auth failures (FR-1.6 / EC-CP-200), NVS-corruption recovery
(EC-NVS-200), "stale" dimming (FR-4.9), and the host-runnable
`mock_display` profile (FR-5.3 / AT-2) — see FSD § 3.3.
