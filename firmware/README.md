# BurnScope Firmware

ESP-IDF firmware for the Cheap Yellow Display (CYD, cyd2usb variant).
Phase 1 — boot + networking — corresponds to FSD § 3.1.

## Prerequisites

- ESP-IDF v6.0.1 at `~/.espressif/v6.0.1/esp-idf` (run its
  `export.sh`/`export.fish` to put `idf.py` on `$PATH`).
- A 2.4 GHz WPA2/WPA3-PSK network the device can reach.

## One-time setup

Phase 1 reads WiFi credentials from a private compile-time header. Copy
the template, fill it in, and **do not commit it** (it's gitignored).

```sh
cp main/wifi_creds.h.example main/wifi_creds.h
$EDITOR main/wifi_creds.h
```

## Build, flash, monitor

```sh
idf.py set-target esp32
idf.py build
idf.py -p <PORT> flash monitor
```

On a healthy boot the panel walks through:

```
Booting…  ──►  Connecting…  ──►  Waiting for daemon…
```

## Verifying Phase 1

| Check         | Command / action                                                                  |
|---------------|------------------------------------------------------------------------------------|
| mDNS visible  | `dns-sd -B _burnscope._tcp` (macOS) — service appears within a few seconds.       |
| Health route  | `curl http://burnscope-XXXX.local/health` — JSON with `firmware_version`, `uptime_s`, `free_heap_b`. |
| Summary route | `curl -i -X POST http://burnscope-XXXX.local/summary -d '{}'` — `204 No Content`. |
| Watchdog      | Build once with `-DBURNSCOPE_WDT_INJECT_HANG`; device reboots within ~10 s, next boot logs `rst:0xc`. |

`XXXX` is the last 4 hex digits of the WiFi STA MAC, lowercased.

## Out of scope for Phase 1

Captive-portal provisioning, `AgentSnapshot` parsing, the full UI with
progress bars and countdowns, `POST /factory-reset`, and the `display_t`
abstraction all live in later phases — see
`../docs/fsd/firmware-fsd.md` § 3.2 / § 3.3.
