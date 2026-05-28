# BurnScope

An always-on token & quota meter for your AI coding agents.

This repo has two components:

- `client/` — Python 3.11+ client. Two collectors with asymmetric lifecycles (see below).
- `firmware/` — ESP32 firmware (ESP-IDF). Two display profiles ship in the
  same tree: the Cheap Yellow Display (CYD, ESP32 + ST7789 320×240) and the
  Waveshare ESP32-S3-Touch-AMOLED-1.43 (466×466 round AMOLED, SH8601/CO5300
  silicon over QSPI). Profile selected by Kconfig at build time.

The wire format (`docs/wire-format.md`) is the daemon ↔ firmware contract — agent-agnostic, hand-mirrored in both languages.

## Multi-agent architecture (client v2)

v2 abandons v1's unified `Agent` ABC + asyncio daemon-loop. Each collector now reads from an agent-native, zero-cost source. Full spec: `docs/client-spec-v2.html`.

- `client/src/burnscope_client/schema.py` — `SessionSnapshot` and `AgentSnapshot` (frozen dataclasses). Unchanged from v1. Matches `docs/wire-format.md`.
- `client/src/burnscope_client/discovery.py` — mDNS browse for `_burnscope._tcp.local.`. `discover_all(timeout, agent=None)` returns every resolved device with per-agent paired hints from TXT records (`paired_claude`, `paired_codex`); pass `agent=…` to filter to devices whose slot is still free.
- `client/src/burnscope_client/identity.py` — Returns Claude's plaintext identifier for the `X-BurnScope-Client-Id` header. `claude_user_identifier()` prefers `oauthAccount.emailAddress` from `~/.claude.json`, falls back to top-level `userID`. No hashing — the ESP32 displays the value on screen. Codex's email comes via the app-server `account/read` call in `codex_daemon.py` and is also sent plaintext.
- `client/src/burnscope_client/host_cache.py` — Atomic file-backed state in `~/.burnscope/` (override with `BURNSCOPE_STATE_DIR`): per-agent `paired-devices.{agent}.json` holds the list of claimed displays (`{device_id, host}` entries); `last-push.{agent}` is the aggregate indicator; `last-push.{agent}.{device_id}` is the per-device outcome (with a `consecutive_failures` counter for diagnostics only — never used for eviction); `mdns-reconcile.{agent}.json` is the per-`device_id` mDNS-reconciliation cooldown map (default 60 s, override via `BURNSCOPE_MDNS_RECONCILE_COOLDOWN_S`). Three mutating primitives on the paired list: `add_paired_device` (insert-or-update, used only by auto-pair), `update_paired_device_host` (update-only — returns False if the device was concurrently unpaired, used by all mDNS reconciliation paths to avoid resurrecting forgotten pairings), and `remove_paired_device` (only called on `/summary` 401 or by `pair-reset`). `claim_reconcile_slots(agent, device_ids)` atomically filters to the device_ids whose cooldown has expired and marks them just-reconciled — concurrent statusline children racing the same device get exactly one winner.
- `client/src/burnscope_client/pusher.py` — `POST /summary` and `GET /health`, both sending the client-id header. `push_to_all()` fans out to a list of `PairedDevice`s in parallel and returns one `PushResult` per device. `PushAuthError` (401) is distinct from `PushError` (transport / other) so callers know whether to silently drop the device or just record a per-device failure. `refresh_and_retry_transport_failures()` gates each transport-failed device through `claim_reconcile_slots`, browses mDNS once, commits any refreshed host via `update_paired_device_host`, and retries the push at the new host. `reconcile_duplicate_hosts()` detects shared `host` values in the paired list, runs one throttled mDNS pass (sharing the same cooldown), and returns the set of device_ids whose cached host still aliases another paired entry — the caller must not mark those healthy from a shared HTTP response.
- `client/src/burnscope_client/claude_statusline.py` — Claude Code statusline hook. Foreground mode renders the line and forks a detached `--push` child; `--push` mode loads the agent's paired list (auto-pairs on first-run if empty), runs fan-out push, runs `refresh_and_retry_transport_failures` then `reconcile_duplicate_hosts`, silently drops devices on 401, and writes per-device + aggregate `last-push.claude*`. Transport / health / identity failures never evict — only `/summary` 401 may remove a pairing.
- `client/src/burnscope_client/codex_daemon.py` — Long-lived daemon owning a `codex app-server` subprocess. Reader/pusher/health/poll coroutines; the pusher fans out across the codex paired list, runs the same `refresh_and_retry_transport_failures` + `reconcile_duplicate_hosts` chain as the statusline, and advances `_last_pushed_snapshot` on any-device success. The poll loop calls `account/rateLimits/read` every 60 s, runs `_anchor_resets_at(fresh, _last_pushed_snapshot)` to paper over codex's wall-clock-driven `resetsAt` drift (rewrites `resets_at` to the last-pushed value when `used_pct` is unchanged so burn-in idle isn't broken by minute-by-minute pushes), then dedupes via `AgentSnapshot.semantically_equal`. The health loop probes each paired device, verifies the response's `device_id` matches the expected paired record (mismatch = identity conflict → routed through reconciliation), runs one shared throttled mDNS browse for the failure batch with single-retry at any refreshed host, then runs `reconcile_duplicate_hosts` on the post-recovery list, and compares the body against `_last_pushed_snapshot` to re-push only to diverged devices. Per-device `_push_failures` / `_health_failures` counters are diagnostic-only — no eviction on either path. Each codex session ships with `rolling=True` and `window_duration_mins` so the firmware can synthesise a fresh countdown at idle. Reconnects with exponential backoff on EOF.
- `client/src/burnscope_client/cli.py` — Installer/status tool, not a daemon entry. Subcommands: `install/uninstall {claude,codex}`, `status` (per-agent, per-device breakdown), `pair` (explicit re-discovery; merges newly-free devices into the paired list — the TOFU claim itself fires on the next push, optional `--agent`), `pair-reset` (wipes both agents' paired-device lists, push state, client-id caches, and reconcile cooldown state), and `ota <bin> --device <id>` (push a firmware image to a paired device's inactive slot — see `docs/wire-format.md` § `POST /ota`).

To add an agent: write a per-fire script (Claude-style) or a long-lived daemon module (Codex-style). The shared modules are `schema`, `discovery`, `host_cache`, `identity`, `pusher`. Register a CLI install path that wires the supervisor (settings.json for Claude-style; launchd/systemd for daemon-style).

## Firmware

Two boards ship in the same image, distinguished by Kconfig profile:

- **Cheap Yellow Display (CYD)** — `cyd2usb` variant (one USB-C and one
  micro-USB port), ESP32, ST7789 320×240 landscape. Code under
  `firmware/main/displays/cyd2usb_st7789/`.
- **Waveshare ESP32-S3-Touch-AMOLED-1.43** — 466×466 round AMOLED panel.
  Driven over QSPI via Espressif's `esp_lcd_sh8601` managed component;
  Waveshare dual-sources the silicon between SH8601 and CO5300, both of
  which speak the same protocol. Code under
  `firmware/main/displays/amoled_sh8601/`.

The `esp32` target defaults to CYD; the `esp32s3` target defaults to AMOLED
(see `firmware/sdkconfig.defaults.<target>`). Override via
`idf.py menuconfig → BurnScope display`.

### Partition layouts

- **CYD (4 MB)** — `partitions-4mb.csv`. Two 2 MB OTA slots, no
  data partition.
- **AMOLED (16 MB)** — `partitions-16mb.csv`. Two 5 MB OTA slots plus
  a ~5.8 MB `storage` partition mounted as LittleFS at `/storage` by
  `firmware/main/storage.c`. The volume is reserved for the future
  pixel-aging map and any persistent assets that don't fit in NVS.

The bootloader build enables `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`
on the AMOLED target so a bad OTA can't brick the device — the
firmware marks the image valid only after the first authorized
`/summary` push succeeds end-to-end (see `maybe_mark_ota_valid` in
`firmware/main/http_server.c`).

### OTA

`POST /ota` (firmware) + `burnscope ota <bin> --device <id>` (CLI)
push a firmware image to a paired device's inactive slot. Auth is
the same `X-BurnScope-Client-Id` slot match as `/summary`, but
`/ota` never TOFU-binds — the device must have been paired by a
prior `/summary` push first. See `docs/wire-format.md` § `POST /ota`.

## ESP-IDF

Path: `~/.espressif/v6.0.1/esp-idf`

## Versioning

This project follows [semantic versioning](https://semver.org/) (MAJOR.MINOR.PATCH):

- **MAJOR** (`1.0.0`): bumped when a milestone is completed
- **MINOR** (`0.1.0`): bumped when a feature PR is accepted
- **PATCH** (`0.0.1`): bumped when a bug fix PR is accepted

## Pointers

- Code style: [./rules/code-style.md](./rules/code-style.md)
- Project description: [../docs/description.md](../docs/description.md)
- Wire format: [../docs/wire-format.md](../docs/wire-format.md)
- Client spec v2 (current): [../docs/client-spec-v2.html](../docs/client-spec-v2.html)
- Claude statusline source: [../docs/claude-statusline.html](../docs/claude-statusline.html)
- Codex app-server source: [../docs/codex-app-server.html](../docs/codex-app-server.html)
- Client spec v1 (historical): [../docs/client-spec.html](../docs/client-spec.html)
