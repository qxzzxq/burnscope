# BurnScope

> An always-on token & quota meter for your AI coding agents.

BurnScope surfaces rate-limit usage for your AI coding agents on a dedicated
ESP32 desk display. Per-agent collectors on your laptop read the session data
the agent **already produces** (no upstream API calls, no header scraping),
discover the ESP32 over mDNS, and push the latest numbers straight to it over
HTTP. No intermediate server.

When you hit the cap and walk away, an always-on display tells you "how much
have I used / when does it reset" without unlocking a laptop.

## Supported agents

| Agent | Source on your laptop | Push lifecycle | Windows |
| --- | --- | --- | --- |
| **Claude Code** | Statusline hook reading `rate_limits.*` from the runtime payload | Per-fire (after each assistant message) | `current` (5 h), `weekly` (7 d) — fixed, anchored to first prompt of the period |
| **Codex CLI** | Long-lived daemon over `codex app-server` JSON-RPC | 60 s poll of `account/rateLimits/read`, anchored on `used_pct` so wall-clock-driven `resetsAt` drift doesn't trigger pushes + 30 s `/health` divergence reconciliation | `primary` (5 h), `secondary` (7 d) — both rolling against wall-clock |

Each collector ships its own plaintext identifier (`oauthAccount.emailAddress`
for Claude, `account.email` for Codex) in `X-BurnScope-Client-Id`. The ESP32
binds it on first push (TOFU) and rejects mismatches with `401`.

## Architecture

```
┌───────────────────────────────┐       POST /summary           ┌────────────────────┐
│  Per-agent collectors         │ ────────────────────────────▶ │  ESP32 display     │
│   - Claude statusline hook    │   AgentSnapshot JSON          │   - mDNS advert    │
│   - Codex app-server daemon   │   + X-BurnScope-Client-Id     │   - HTTP server    │
│  Shared: schema, discovery,   │                               │   - NVS pairing    │
│  identity, pusher, host_cache │                               │   - LVGL renderer  │
└───────────────────────────────┘                               └────────────────────┘
```

See:

- [`docs/description.md`](./docs/description.md) — design rationale & scope.
- [`docs/wire-format.md`](./docs/wire-format.md) — daemon ↔ firmware contract (TOFU pairing, monotonic `captured_at`, `/health` shape, error codes).
- [`docs/client-spec-v2.html`](./docs/client-spec-v2.html) — canonical v2 client spec.
- [`docs/fsd/firmware-fsd.md`](./docs/fsd/firmware-fsd.md) — firmware functional spec.

(The historical v1 client spec lives at `docs/client-spec.html` for reference.)

## Installing the client

The CLI entry point is `burnscope` (installer/status only, not a daemon
entry). Install it with [uv](https://docs.astral.sh/uv/) — `client/uv.lock`
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

`install` is per-agent — install only the ones you use. The status command
prints the cached ESP32 host, identifier source per agent, and the last-push
indicator under `~/.burnscope/`.

Debugging tips and log-level controls live in [`client/README.md`](./client/README.md).

## Building & flashing the firmware

Two display boards are supported; the target chip picks the default
display profile automatically.

```sh
. ~/.espressif/v6.0.1/esp-idf/export.sh   # once per shell
cd firmware                                # all commands below run from here

idf.py set-target esp32                    # CYD (cyd2usb, ST7789 320×240)
# or
idf.py set-target esp32s3                  # Waveshare 1.43" AMOLED (SH8601/CO5300, 466×466)

idf.py -p <PORT> flash monitor             # builds, writes, then tails serial
```

First boot brings up a captive portal (`BURNSCOPE-XXXX` open AP) for WiFi.
Full details, re-provisioning, and on-device smoke tests in
[`firmware/README.md`](./firmware/README.md).

After the initial USB flash, subsequent updates can ship over the LAN
via `burnscope ota <bin> --device <device_id>` (see `POST /ota` in
[`docs/wire-format.md`](./docs/wire-format.md)). The AMOLED uses a
16 MB layout with two 5 MB OTA slots, a ~5.8 MB LittleFS volume at
`/storage` (room for the future pixel-aging map), and bootloader
rollback enabled — a bad image is reverted automatically if it fails
to mark itself valid on first boot. The CYD stays on its 4 MB layout
with two ~1.875 MB OTA slots; it accepts the same `burnscope ota`
push but does not run the rollback safety net (a corrupted image
requires a USB re-flash to recover).

## Repository layout

```
burnscope/
├── README.md
├── .claude/CLAUDE.md             ← in-repo agent instructions
├── docs/
│   ├── description.md            ← design rationale
│   ├── wire-format.md            ← daemon ↔ firmware contract
│   ├── client-spec-v2.html       ← v2 client spec (current)
│   ├── client-spec.html          ← v1 client spec (historical)
│   ├── claude-statusline.html    ← Claude Code statusline reference
│   ├── codex-app-server.html     ← codex app-server reference
│   └── fsd/firmware-fsd.md       ← firmware functional spec
├── client/                       ← Python collectors (v2)
│   └── src/burnscope_client/
│       ├── schema.py             ← SessionSnapshot / AgentSnapshot
│       ├── discovery.py          ← mDNS browse for _burnscope._tcp.local
│       ├── identity.py           ← plaintext client_id resolver
│       ├── host_cache.py         ← atomic ~/.burnscope/ state
│       ├── pusher.py             ← POST /summary, GET /health
│       ├── ota_pusher.py         ← POST /ota (firmware update over LAN)
│       ├── claude_statusline.py  ← Claude Code statusline hook (per-fire)
│       ├── codex_daemon.py       ← long-lived Codex daemon
│       └── cli.py                ← install/uninstall/status/pair{-reset}/ota
└── firmware/                     ← ESP32 firmware (ESP-IDF; CYD ST7789 + Waveshare 1.43" AMOLED)
```

## Adding a new agent

There's no single Agent ABC to subclass. Each agent has its own lifecycle:

- **Statusline-style (per-fire)** — write a script like
  `claude_statusline.py` that builds an `AgentSnapshot` and forks a detached
  `--push` child. Register a `burnscope-client install <agent>` path that
  wires the supervisor (e.g. `settings.json` for Claude Code).
- **Daemon-style (long-lived)** — write a module like `codex_daemon.py`
  that owns its upstream subprocess, queues snapshots, and pushes them.
  Register a `burnscope-client install <agent>` path that drops a launchd
  plist / systemd unit.

In both cases reuse the shared modules: `schema` (frozen dataclasses,
matches the wire format), `discovery` (mDNS), `host_cache` (atomic state),
`identity` (resolves the plaintext identifier sent in the header), and
`pusher` (the actual HTTP call). The firmware contract is agent-agnostic;
all the per-agent work is upstream of `schema.AgentSnapshot`.
