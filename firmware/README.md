# BurnScope Firmware

ESP-IDF firmware for the Cheap Yellow Display (CYD, cyd2usb variant).
Phase 2 — captive-portal provisioning + snapshot rendering — corresponds
to FSD § 3.2.

## Prerequisites

- ESP-IDF v6.0.1 at `~/.espressif/v6.0.1/esp-idf` (run its
  `export.sh`/`export.fish` to put `idf.py` on `$PATH`).
- A 2.4 GHz WPA2/WPA3-PSK network the device can reach.

## Build, flash, monitor

```sh
idf.py set-target esp32
idf.py build
idf.py -p <PORT> flash monitor
```

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
`sdkconfig.defaults`. Phase 2 ships one profile:

| Kconfig symbol                            | Profile path                       |
|-------------------------------------------|------------------------------------|
| `CONFIG_BURNSCOPE_DISPLAY_CYD2USB_ST7789` | `main/displays/cyd2usb_st7789/`    |

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
