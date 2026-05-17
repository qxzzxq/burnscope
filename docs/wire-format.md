# BurnScope wire format (MVP)

The shared contract spoken by the daemon, server, and ESP32 firmware. JSON over
HTTP, UTF-8. Two endpoints, two payload shapes.

This document is the source of truth — each language hand-writes its own types
to match. Examples live alongside as `examples/event.json` and
`examples/summary.json` and are kept in sync by hand.

---

## Types

### `WindowSnapshot`

One rolling-quota window at one point in time.

| Field       | Type    | Range / format     | Description |
|-------------|---------|--------------------|-------------|
| `used_pct`  | float   | `0.0` – `1.0`      | Fraction of the window used. `0.03` = 3%. |
| `resets_at` | integer | unix seconds (UTC) | When the window rolls over. Clients compute the countdown locally against their own (NTP-synced) clock. |

### `AgentSnapshot`

Both windows for one agent at one point in time.

| Field         | Type             | Description |
|---------------|------------------|-------------|
| `agent`       | string enum      | One of `"claude"`, `"codex"`. |
| `captured_at` | integer (unix s) | When the daemon read the headers from the upstream API. |
| `window_5h`   | `WindowSnapshot` | The 5-hour rolling window. |
| `window_7d`   | `WindowSnapshot` | The 7-day rolling window. |

Field names hardcode `5h` and `7d` because both supported agents use exactly
these window sizes. If a future agent ships different windows, that becomes a
deliberate schema change.

---

## `POST /api/events`

Daemon → server. One snapshot per request. Sent on every poll, even if values
haven't changed.

**Request body:** a single `AgentSnapshot` (see `examples/event.json`).

**Response:** `204 No Content` on success.

Server overwrites its in-memory cache keyed by `agent`. No history is kept in
MVP. No auth (LAN trust).

---

## `GET /api/summary/window`

ESP32 (or web client) → server. Returns the latest snapshot the server has
received for every agent.

**Response body:** an object (see `examples/summary.json`).

| Field         | Type                | Description |
|---------------|---------------------|-------------|
| `server_time` | integer (unix s)    | The server's current wall-clock time. Lets the client sanity-check its own clock and compute "last update Xs ago" without making the server pre-compute it. |
| `agents`      | array of `AgentSnapshot` | Zero, one, or two entries. Order is not guaranteed; clients look up by the `agent` field. |

Clients must handle an empty `agents` array (nothing has reported yet).

---

## Header → field mapping

For collector implementers. Source of these headers: `docs/probe-claude.sh`
and `docs/probe-codex.sh`.

| Schema field          | Claude Code header                              | Codex CLI header                          |
|-----------------------|-------------------------------------------------|-------------------------------------------|
| `window_5h.used_pct`  | `anthropic-ratelimit-unified-5h-utilization`    | `x-codex-primary-used-percent` ÷ 100      |
| `window_5h.resets_at` | `anthropic-ratelimit-unified-5h-reset`          | `x-codex-primary-reset-at`                |
| `window_7d.used_pct`  | `anthropic-ratelimit-unified-7d-utilization`    | `x-codex-secondary-used-percent` ÷ 100    |
| `window_7d.resets_at` | `anthropic-ratelimit-unified-7d-reset`          | `x-codex-secondary-reset-at`              |

Claude returns `used_pct` already as a `0.0`–`1.0` float; Codex returns
`0`–`100` integers and the collector divides by 100.

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
  beyond 5h+7d. Not shown on the MVP display.
- **Plan metadata.** Codex exposes `x-codex-plan-type` and
  `x-codex-active-limit`; Claude exposes `unified-status` and
  `unified-fallback-percentage`. Useful for UI polish, not for the MVP numbers.
- **Historical aggregates.** No `/api/summary/daily`, no per-session breakdown.
  Cut during the description-doc trim — server keeps only the latest snapshot
  per agent.
- **Cost/dollar estimates.** Token-based metrics only.
- **Auth.** `POST /api/events` accepts pushes from any LAN client. A shared
  secret is straightforward to add later.
