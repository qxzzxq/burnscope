# BurnScope Firmware

ESP-IDF firmware for three display boards:

- **Cheap Yellow Display (CYD)**: ESP32, ST7789 320×240 LCD.
- **Waveshare ESP32-S3-Touch-AMOLED-1.43"**: 466×466 round AMOLED (SH8601/CO5300 over QSPI).
- **Waveshare ESP32-S3-Touch-AMOLED-1.75"**: 466×466 round CO5300 AMOLED; reuses the 1.43"'s UI and adds CST9217 touch plus an AXP2101 PMU.

One image holds all three profiles; pick one at build time. Per-board feature
support is in the [top-level README](../README.md).

## Prerequisites

- ESP-IDF v6.0.1 at `~/.espressif/v6.0.1/esp-idf` (run its `export.sh` or `export.fish` to put `idf.py` on `$PATH`).
- A 2.4 GHz WPA2/WPA3-PSK network the device can reach.

## Build, flash, monitor

```sh
. ~/.espressif/v6.0.1/esp-idf/export.sh   # once per shell
cd firmware

idf.py set-target esp32                    # CYD
# or
idf.py set-target esp32s3                  # AMOLED 1.43"

idf.py -p <PORT> flash monitor             # builds, writes, then tails serial
```

`flash` auto-builds, so there's no separate `build` step. The target chip sets
the default profile (esp32 → CYD, esp32s3 → AMOLED 1.43"); switch within a
target via `idf.py menuconfig → BurnScope display`. On macOS the CYD enumerates
as `/dev/cu.usbserial-XXXX` (CH340) and the AMOLED as `/dev/cu.usbmodemNNNN`.

The 1.75" AMOLED shares the esp32s3 target with the 1.43", so it builds from
its own sdkconfig fragment and build directory:

```sh
idf.py -B build-amoled175 -DIDF_TARGET=esp32s3 \
  -DSDKCONFIG=build-amoled175/sdkconfig \
  -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.esp32s3;sdkconfig.amoled175" \
  build
idf.py -B build-amoled175 -p <PORT> flash monitor
```

No WiFi credentials are baked into the image; the device captures them over a
captive portal on first boot.

## First-time provisioning

On a fresh (or freshly reset) device:

1. The panel shows a setup prompt naming the `BURNSCOPE-XXXX` network, where `XXXX` is the last four hex digits of the board's MAC.
2. From a phone or laptop, join that open WiFi network.
3. The captive-portal popup opens the form automatically. If it doesn't, browse to <http://192.168.4.1/>.
4. Pick your network, enter the password, and hit **Save & reboot**.
5. The device persists the credentials to NVS and restarts. Later boots skip the portal and join directly.

The panel walks `Booting… → Connecting… → Waiting for daemon…`, then switches
to the agent screen on the first `POST /summary`.

## Re-provisioning

Long-press the BOOT button (GPIO0) for at least 5 s. The device wipes its WiFi
credentials and pairing slots and reboots into the captive portal; the next
`POST /summary` from any laptop rebinds verbatim (TOFU). A network factory-reset
endpoint isn't exposed yet (the route had no auth and was pulled), so the BOOT
button is the only path for now.

## Display profiles

Each profile bundles a panel driver with its UI layout under
`main/displays/<name>/`, chosen by Kconfig (`menuconfig → BurnScope display`)
and pinned in `sdkconfig.defaults` or the target overlay.

| Kconfig symbol | Target | Profile path |
| --- | --- | --- |
| `CONFIG_BURNSCOPE_DISPLAY_CYD2USB_ST7789` | esp32 | `main/displays/cyd2usb_st7789/` |
| `CONFIG_BURNSCOPE_DISPLAY_AMOLED_SH8601` | esp32s3 | `main/displays/amoled_sh8601/` |
| `CONFIG_BURNSCOPE_DISPLAY_AMOLED_CO5300_175` | esp32s3 | `main/displays/amoled_co5300_175/` |

All profiles share the wire format and snapshot store; only the rendering
changes (linear bars on the CYD, concentric arcs on the AMOLEDs). The AMOLED
design spec is `docs/ui/amoled_sh8601.md`, and layouts are tuned with the
browser tools in `scripts/` (`amoled-preview.html`, `font-preview.html`).

To add a screen, drop `driver.c`, `ui.c`, `Kconfig`, and `sources.cmake` into
`main/displays/<name>/`, add one `rsource "displays/<name>/Kconfig"` line to the
`BURNSCOPE_DISPLAY` choice in `main/Kconfig.projbuild`, then run
`idf.py reconfigure`.

The Waveshare vendor demo bundle isn't vendored here (it's 346 MB; the bits we
use are trimmed into `main/displays/amoled_sh8601/` with attribution). To follow
the in-repo references under `docs/ESP32-S3-AMOLED-1.43-Demo/`, download it from
[Waveshare's product page](https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.43/ESP32-S3-AMOLED-1.43-Demo-V3.zip)
and unzip it into `docs/` (the directory is gitignored).

## Smoke test

```sh
HOST=burnscope-XXXX.local        # or the IP if mDNS is blocked
./scripts/phase2_smoke.sh "$HOST"
```

The script drives the `POST /summary` and `/health` cases. The full test matrix
and the manual visual checks live in the FSD
([`../docs/fsd/firmware-fsd.md`](../docs/fsd/firmware-fsd.md), § 8).
