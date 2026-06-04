# BurnScope

> An always-on token & quota meter for your AI coding agents.

BurnScope surfaces rate-limit usage for your AI coding agents on a dedicated
ESP32 desk display. Per-agent collectors on your laptop read the session data
the agent **already produces** (no upstream API calls, no header scraping),
discover the ESP32 over mDNS, and push the latest numbers straight to it over
HTTP. No intermediate server.

When you hit the cap and walk away, the display tells you how much you've used
and when it resets, without unlocking a laptop.

## Supported agents

| Agent | Source on your laptop | Push lifecycle | Windows |
| --- | --- | --- | --- |
| **Claude Code** | Statusline hook reading `rate_limits.*` from the runtime payload | Per-fire (after each assistant message) | `current` (5 h) and `weekly` (7 d), both fixed and anchored to the first prompt of the period |
| **Codex CLI** | Long-lived daemon over `codex app-server` JSON-RPC | 60 s poll of `account/rateLimits/read`, anchored on `used_pct` so wall-clock-driven `resetsAt` drift doesn't trigger pushes, plus 30 s `/health` divergence reconciliation | `primary` (5 h) and `secondary` (7 d), both rolling against wall-clock |

Each collector sends its own plaintext identifier (`oauthAccount.emailAddress`
for Claude, `account.email` for Codex) in `X-BurnScope-Client-Id`. The ESP32
binds it on first push (TOFU) and rejects mismatches with `401`.

## Supported hardware

Three ESP32 display boards build from the same firmware tree; pick one at
build time (see [Building & flashing](#building--flashing-the-firmware)).

- **Cheap Yellow Display (CYD)**: ESP32, ST7789 320×240 LCD, 4 MB flash.
- **Waveshare ESP32-S3-Touch-AMOLED-1.43"**: ESP32-S3, SH8601/CO5300 466×466 round AMOLED, 16 MB flash.
- **Waveshare ESP32-S3-Touch-AMOLED-1.75"**: ESP32-S3, CO5300 466×466 round AMOLED, 16 MB flash.

What each board does today (✓ implemented, ✗ not available):

| Board | Agent display | Auto-rotate | Burn-in guard | Touch | Battery | OTA | OTA rollback |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| CYD | ✓ | ✗ | ✗ | ✗ | ✗ | ✓ | ✗ |
| AMOLED 1.43" | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ | ✓ |
| AMOLED 1.75" | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

Burn-in guard, auto-rotate, and touch are AMOLED-only; the CYD's LCD doesn't
age and uses a fixed rotation. Battery telemetry needs the 1.75" board's
AXP2101 PMU. OTA rollback rides on the ESP32-S3 bootloader, so the CYD takes
LAN updates but without the auto-revert safety net. Details are in
[`docs/architecture.html`](./docs/architecture.html).

## Installing the client

The CLI entry point is `burnscope` (installer and status only, not a daemon
entry). Install it with [uv](https://docs.astral.sh/uv/); `client/uv.lock`
pins the deps.

```sh
uv tool install ./client                              # installs `burnscope` on PATH
burnscope install claude                              # writes Claude Code statusline hook
burnscope install codex                               # launchd (macOS) or systemd --user (Linux)
burnscope status                                      # confirm wiring + last push outcomes
burnscope pair                                        # rediscover unpaired devices on the LAN
burnscope pair-reset                                  # forget cached host + per-agent client_ids
burnscope ota firmware/build-s3/burnscope.bin \
    --device burnscope-XXXX                           # push a firmware image over the LAN
```

For an editable install (source changes picked up automatically):

```sh
uv tool install --editable ./client
```

`install` is per-agent, so install only the ones you use. The status command
prints the cached ESP32 host, the identifier source per agent, and the
last-push indicator under `~/.burnscope/`. Debugging tips and log-level
controls live in [`client/README.md`](./client/README.md).

## Building & flashing the firmware

The target chip picks the default display profile. The 1.75" AMOLED shares the
ESP32-S3 target with the 1.43" and selects its profile through a dedicated
sdkconfig.

```sh
. ~/.espressif/v6.0.1/esp-idf/export.sh   # once per shell
cd firmware                                # all commands below run from here

idf.py set-target esp32                    # CYD (ST7789 320×240)
# or
idf.py set-target esp32s3                  # Waveshare AMOLED 1.43"

idf.py -p <PORT> flash monitor             # builds, writes, then tails serial
```

For the **1.75" AMOLED**, see the exact `-B build-amoled175 …` invocation in
[`firmware/README.md`](./firmware/README.md).

First boot brings up a captive portal (`BURNSCOPE-XXXX` open AP) for WiFi.
Provisioning, re-provisioning, and on-device smoke tests are covered in
[`firmware/README.md`](./firmware/README.md).

After the initial USB flash, later updates ship over the LAN with
`burnscope ota <bin> --device <device_id>` (see `POST /ota` in
[`docs/wire-format.md`](./docs/wire-format.md)). Per-board partition layouts
and the bootloader rollback safety net are covered in
[`docs/architecture.html`](./docs/architecture.html).

## Architecture

A per-agent collector reads the data your agent already writes, finds the
ESP32 over mDNS, and pushes it over HTTP. There is no intermediate server.
The wire format is agent-agnostic, so the firmware renders any snapshot
without knowing which agent produced it.

See [`docs/architecture.html`](./docs/architecture.html) for the full design.
Reference docs:

- [`docs/description.md`](./docs/description.md): design rationale and scope.
- [`docs/wire-format.md`](./docs/wire-format.md): the daemon ↔ firmware contract (TOFU pairing, monotonic `captured_at`, `/health` shape, error codes).
- [`docs/client-spec-v2.html`](./docs/client-spec-v2.html): the canonical v2 client spec.
- [`docs/fsd/firmware-fsd.md`](./docs/fsd/firmware-fsd.md): the firmware functional spec.

## Repository layout

```
burnscope/
├── client/      ← Python collectors (Claude statusline + Codex daemon)
├── firmware/    ← ESP32 firmware (ESP-IDF; CYD + AMOLED profiles)
├── docs/        ← specs, wire format, architecture
└── .claude/     ← in-repo agent instructions
```

## Adding a new agent

There's no single `Agent` base class to subclass. Each agent reads from its
own native source, upstream of `schema.AgentSnapshot`. See
[`docs/add_an_agent.html`](./docs/add_an_agent.html) for the two lifecycle
patterns (statusline per-fire and long-lived daemon) and a step-by-step guide.
