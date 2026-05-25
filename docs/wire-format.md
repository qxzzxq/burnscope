# BurnScope wire format (MVP)

The shared contract spoken by the Python daemon and the ESP32 firmware. JSON
over HTTP, UTF-8. One endpoint, one payload shape.

This document is the source of truth — each language hand-writes its own types
to match. An example payload lives alongside as `examples/summary-push.json`
and is kept in sync by hand.

---

## Types

### `SessionSnapshot`

One rate-limit session (quota window) at one point in time.

| Field                  | Type    | Range / format     | Description |
|------------------------|---------|--------------------|-------------|
| `type`                 | string  | agent-defined      | Label for this session, in the upstream agent's own vocabulary. See the per-agent vocabulary table below. |
| `used_pct`             | float   | `0.0` – `1.0`      | Fraction of the session used. `0.03` = 3%. |
| `resets_at`            | integer | unix seconds (UTC) | When the session rolls over. Clients compute the countdown locally against their own (NTP-synced) clock. |
| `rolling`              | bool    | `true` / `false`   | `true` if the underlying window's reset time drifts with wall-clock during idle (Codex). `false` if it is anchored to the first usage event and stays put until expiry (Claude). Lets the firmware decide whether to trust the pushed `resets_at` as-is or synthesize a fresh countdown locally during idle. |
| `window_duration_mins` | integer | minutes (≥ 0)      | Total length of the window. Used by the firmware to synthesize a countdown locally when `rolling` is `true` and `used_pct` is essentially zero; otherwise informational. `0` is allowed and means "unknown / synthesis not desired". |

### `AgentSnapshot`

All sessions reported by one agent at one point in time.

| Field         | Type             | Description |
|---------------|------------------|-------------|
| `agent`       | string enum      | One of `"claude"`, `"codex"`. |
| `captured_at` | integer (unix s) | When the daemon read the headers from the upstream API. |
| `sessions`    | array of `SessionSnapshot` | One or more session entries. Order is not guaranteed; clients look up by `type`. |

Each agent uses its own vocabulary for `type` (Claude reports
`current`/`weekly` — derived from Anthropic's `5h`/`7d` rate-limit
windows; Codex reports `primary`/`secondary`); the daemon passes those
labels through untouched. Clients should render any `type` they receive
— including ones they don't recognise — using the raw string as the
label.

---

## `POST /summary`

Daemon → ESP32. One snapshot per request. **Edge-triggered**: the daemon
only POSTs when the content of an agent's snapshot — `(agent, sessions)`,
ignoring `captured_at` — differs from what the firmware currently holds.
The firmware is the source of truth: every cycle the daemon `GET`s
`/health` (see below) to read the device's stored `sessions` and POSTs
whenever its latest probe diverges. This self-heals after an ESP32
reboot (RAM-only store wiped → next cycle re-POSTs), a daemon restart,
or any other event that desyncs the two sides.

**Request body:** a single `AgentSnapshot` (see `examples/summary-push.json`).

**Response:** `204 No Content` on success.

The firmware overwrites its in-memory "latest snapshot" for that
`agent` on receipt. The screen swaps off the "waiting for daemon..."
splash on the very first push; subsequent pushes only update the
in-memory store — the firmware's 1 Hz LVGL tick re-renders the
currently visible agent against the updated store and handles
between-agent cycling. This means a Codex push does not yank rotation
away from a currently-visible Claude row (and vice versa). No history
is kept. The ESP32 syncs its wall-clock over NTP and computes "resets
in X" locally against `resets_at` — there is no server timestamp on
the wire.

### Headers

| Header                   | Required | Description |
|--------------------------|----------|-------------|
| `Content-Type`           | yes      | `application/json` |
| `X-BurnScope-Client-Id`  | yes      | Plaintext per-agent identifier. Claude collectors send `oauthAccount.emailAddress` (or top-level `userID` as fallback) from `~/.claude.json`; the Codex collector sends `account.email` from the app-server `account/read` response. Value is ASCII-printable, no control characters, length ≤ 254 bytes (RFC 5321 mailbox cap). The ESP32 stores it verbatim and renders it as the owner label per agent. Authorisation is trust-on-first-use: an empty pairing slot accepts and persists the header; subsequent pushes must match (mismatch → `401 Unauthorized`). Re-pair via the BOOT-button long-press / AP-mode reprovision flow. |

### Error responses

| Status | Body | When |
|--------|------|------|
| `204 No Content`        | empty | Snapshot accepted. |
| `400 Bad Request`       | text/plain reason | Empty body, malformed JSON, unknown agent, or schema violation. |
| `401 Unauthorized`      | `{"error":"client id required"}` or `{"error":"client id mismatch"}` | Missing/empty header, or header doesn't match the bound id for this agent. |
| `409 Conflict`          | `{"error":"stale captured_at"}` | Incoming `captured_at` is strictly older than the stored snapshot's. Idempotent equal values are accepted. Multi-laptop / multi-session deployments converge to the freshest data. |
| `413 Content Too Large` | text/plain | Body exceeds the 16 KiB cap. |

## `GET /health`

Daemon → ESP32. Called every cycle. Drives both liveness detection
(failure → drop the mDNS-cached host and rediscover) and edge-trigger
reconciliation (compare firmware-side `sessions` against the latest
probe; re-POST `/summary` on divergence).

Response is JSON:

| Field              | Type    | Description |
|--------------------|---------|-------------|
| `firmware_version` | string  | Built-in version string. |
| `uptime_s`         | integer | Seconds since boot. |
| `free_heap_b`      | integer | Free heap in bytes. |
| `agents`           | object  | Map of agent name → entry (see below). Agents with no stored snapshot — e.g. immediately after a reboot — are omitted; the daemon treats absence as "must push". |

Each entry under `agents`:

| Field                     | Type    | Description |
|---------------------------|---------|-------------|
| `client_id`               | string  | The bound `X-BurnScope-Client-Id` for this agent. Lets the daemon detect drift after a factory reset (slot was cleared → daemon resends to TOFU-rebind). Empty string when no slot was filled at probe time. |
| `seconds_since_last_push` | integer | Age of the stored snapshot in seconds. |
| `sessions`                | array of `SessionSnapshot` | Same shape as `POST /summary`'s `sessions`. Lets the daemon detect when the firmware's stored content has diverged from the latest upstream probe without waiting for a content change to push. |

The firmware does not act on the request beyond responding —
receiving `/health` does not refresh the snapshot store or the display.

Authorisation matches the `/summary` rule: `X-BurnScope-Client-Id`
must equal *any* populated pairing slot. A fresh device with no
bindings accepts header-less probes so the daemon's first contact
succeeds. Mismatches return `401 Unauthorized`.

---

## mDNS advertisement

The firmware advertises one service on the LAN:

- **Service type:** `_burnscope._tcp.local.`
- **Instance name:** `BurnScope XXXX` (where `XXXX` is the last four hex digits of the Wi-Fi MAC, lowercase) — unique per device so `dns-sd -B` shows the LAN cleanly without Bonjour's auto-disambiguation suffixes.
- **Hostname:** `burnscope-XXXX.local.` (same MAC suffix as the instance name).
- **Port:** `80`

### TXT records

| Key             | Value         | Meaning |
|-----------------|---------------|---------|
| `version`       | string        | Firmware version (`BURNSCOPE_FW_VERSION`). |
| `paired_claude` | `0` or `1`    | `1` iff the `claude` NVS pairing slot is non-empty. |
| `paired_codex`  | `0` or `1`    | `1` iff the `codex` NVS pairing slot is non-empty. |

The firmware updates the relevant `paired_<agent>` TXT item on every
slot transition: from `0` to `1` when `authorize_summary` TOFU-binds
a previously-empty slot, and from `1` to `0` when the factory-reset /
re-provision path wipes the slots.

`paired_<agent>` is an **advisory hint** for client filtering. The
authoritative answer is still the HTTP response: a client may try to
POST `/summary` against a device whose TXT advertised `paired_<agent>=0`
and get `401 Unauthorized` if the TXT cache was stale (another client
beat us to the claim). Conversely, a device showing `paired_<agent>=1`
should be skipped — pushing against it will return `401` unless our
client_id happens to match what's stored.

Clients holding stale TXT data due to mDNS caching is normal; they
fall back to the HTTP response (`204` claims, `401` skips).

---

## Upstream → session mapping

For collector implementers. The v2 client no longer scrapes
rate-limit headers from upstream HTTP responses (which cost tokens);
it reads the same numbers from agent-native sources. The mapping each
collector applies before constructing a `SessionSnapshot`:

| Agent    | `sessions[].type` | Source field                                                | `rolling` | `window_duration_mins` | Notes |
|----------|-------------------|-------------------------------------------------------------|-----------|------------------------|-------|
| `claude` | `current`         | Claude Code statusline payload: `rate_limits.five_hour.used_percentage` ÷ 100, `rate_limits.five_hour.resets_at` | `false`   | `300`                  | See `docs/claude-statusline.html`. Anthropic's 5h window is anchored to the first message of the session — `resets_at` stays put until expiry, so synthesis is unnecessary. |
| `claude` | `weekly`          | Claude Code statusline payload: `rate_limits.seven_day.used_percentage` ÷ 100, `rate_limits.seven_day.resets_at` | `false`   | `10080`                | Same source. 7d window is anchored to the first prompt of the week. |
| `codex`  | `primary`         | `codex app-server`: `rateLimits.primary.usedPercent` ÷ 100, `rateLimits.primary.resetsAt` | `true`    | from `rateLimits.primary.windowDurationMins` (typically `300`) | See `docs/codex-app-server.html`. The backend reports `resetsAt` as roughly `now + remaining`, so it drifts with wall-clock during idle — `rolling: true` tells the firmware to synthesize a countdown when `used_pct ≤ 0.01`. |
| `codex`  | `secondary`       | `codex app-server`: `rateLimits.secondary.usedPercent` ÷ 100, `rateLimits.secondary.resetsAt` | `true`    | from `rateLimits.secondary.windowDurationMins` (typically `10080`) | Same source. |

Both upstream sources return percentages on a 0–100 scale; the
collector divides by 100 before constructing the `SessionSnapshot`.

The historical header-probe scripts (`docs/probe-claude.sh`,
`docs/probe-codex.sh`) made real token-costing API calls and are
preserved only as references for the v1 wire mapping.

---

## Deferred (intentionally not in MVP)

These were considered and cut. Documented here so future contributors don't
re-litigate without a reason.

- **`binding` / active-window indicator.** Both Claude
  (`unified-representative-claim`) and a daemon-side heuristic for Codex could
  surface "which window will limit you first." Skipped because the MVP display
  shows both windows simultaneously.
- **Third-tier buckets.** Claude `unified-overage-*` (pay-per-use overage) and
  Codex `x-codex-credits-*` (pay-as-you-go credits) describe a third quota
  beyond Claude's `current`+`weekly` (5h+7d) windows. These fit the current schema — they'd just be an additional
  `sessions[]` entry (e.g. `type: "overage"` or `type: "credits"`) — so the
  daemon and firmware need no contract changes when we wire them up.
  Deferred only because the MVP display doesn't render them.
- **Plan metadata.** Codex exposes `x-codex-plan-type` and
  `x-codex-active-limit`; Claude exposes `unified-status` and
  `unified-fallback-percentage`. Useful for UI polish, not for the MVP numbers.
- **Historical aggregates.** No daily totals, no per-session breakdown. The
  firmware keeps only the latest snapshot per agent.
- **Cost/dollar estimates.** Token-based metrics only.
- **Intermediate aggregation server.** An earlier MVP draft had a Go server
  fronting the firmware. Cut because for one laptop + one display it added
  installs and an always-on process without buying anything. It earns its
  keep in Phase 2 if multi-machine aggregation, non-session schemas
  (credits, overage) needing shared state, or auth arrive — and slots in by
  speaking this same `POST /summary` to the firmware.
