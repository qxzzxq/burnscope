# BurnScope wire format (MVP)

The shared contract spoken by the Python daemon and the ESP32 firmware. JSON
over HTTP, UTF-8. One endpoint, one payload shape.

This document is the source of truth — each language hand-writes its own types
to match. An example payload lives alongside as `examples/summary-push.json`
and is kept in sync by hand.

---

## Types

### `SessionSnapshot`

One rate-limit session (rolling-quota window) at one point in time.

| Field       | Type    | Range / format     | Description |
|-------------|---------|--------------------|-------------|
| `type`      | string  | agent-defined      | Label for this session, in the upstream agent's own vocabulary. See the per-agent vocabulary table below. |
| `used_pct`  | float   | `0.0` – `1.0`      | Fraction of the session used. `0.03` = 3%. |
| `resets_at` | integer | unix seconds (UTC) | When the session rolls over. Clients compute the countdown locally against their own (NTP-synced) clock. |

### `AgentSnapshot`

All sessions reported by one agent at one point in time.

| Field         | Type             | Description |
|---------------|------------------|-------------|
| `agent`       | string enum      | One of `"claude"`, `"codex"`. |
| `captured_at` | integer (unix s) | When the daemon read the headers from the upstream API. |
| `sessions`    | array of `SessionSnapshot` | One or more session entries. Order is not guaranteed; clients look up by `type`. |

Each agent uses its own vocabulary for `type` (Claude reports `5h`/`7d`;
Codex reports `primary`/`secondary`); the daemon passes those labels through
untouched. Clients should render any `type` they receive — including ones
they don't recognise — using the raw string as the label.

---

## `POST /summary`

Daemon → ESP32. One snapshot per request. Sent on every JSONL change and on a
~30 s keepalive so a freshly-booted display catches up without waiting for the
next agent action.

**Request body:** a single `AgentSnapshot` (see `examples/summary-push.json`).

**Response:** `204 No Content` on success.

The firmware overwrites its in-memory "latest snapshot" on receipt and
repaints. No history is kept. No auth (LAN trust). The ESP32 syncs its
wall-clock over NTP and computes "last update Xs ago" locally against
`captured_at` — there is no server timestamp on the wire.

---

## Header → session mapping

For collector implementers. Source of these headers: `docs/probe-claude.sh`
and `docs/probe-codex.sh`.

| Agent    | `sessions[].type` | `used_pct` header                              | `resets_at` header                |
|----------|-------------------|------------------------------------------------|-----------------------------------|
| `claude` | `5h`              | `anthropic-ratelimit-unified-5h-utilization`   | `anthropic-ratelimit-unified-5h-reset` |
| `claude` | `7d`              | `anthropic-ratelimit-unified-7d-utilization`   | `anthropic-ratelimit-unified-7d-reset` |
| `codex`  | `primary`         | `x-codex-primary-used-percent` ÷ 100           | `x-codex-primary-reset-at`        |
| `codex`  | `secondary`       | `x-codex-secondary-used-percent` ÷ 100         | `x-codex-secondary-reset-at`      |

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
  beyond 5h+7d. These fit the current schema — they'd just be an additional
  `sessions[]` entry (e.g. `type: "overage"` or `type: "credits"`) — so the
  daemon and firmware need no contract changes when we wire them up.
  Deferred only because the MVP display doesn't render them.
- **Plan metadata.** Codex exposes `x-codex-plan-type` and
  `x-codex-active-limit`; Claude exposes `unified-status` and
  `unified-fallback-percentage`. Useful for UI polish, not for the MVP numbers.
- **Historical aggregates.** No daily totals, no per-session breakdown. The
  firmware keeps only the latest snapshot per agent.
- **Cost/dollar estimates.** Token-based metrics only.
- **Auth.** `POST /summary` accepts pushes from any LAN client. A shared
  secret is straightforward to add later.
- **Intermediate aggregation server.** An earlier MVP draft had a Go server
  fronting the firmware. Cut because for one laptop + one display it added
  installs and an always-on process without buying anything. It earns its
  keep in Phase 2 if multi-machine aggregation, non-session schemas
  (credits, overage) needing shared state, or auth arrive — and slots in by
  speaking this same `POST /summary` to the firmware.
