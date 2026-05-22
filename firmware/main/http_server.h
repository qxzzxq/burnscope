#pragma once

/*
 * http_server.h — production HTTP routes (STA mode only).
 *
 * Two endpoints on port 80:
 *   POST /summary        — parse one AgentSnapshot per `docs/wire-format.md`,
 *                          put into the snapshot store, respond 204.
 *                          401 missing/mismatched X-BurnScope-Client-Id,
 *                          400 on bad JSON / schema / range, 409 on stale
 *                          captured_at, 413 over 16 KiB.
 *   GET  /health         — JSON: firmware_version, uptime_s, free_heap_b,
 *                          agents[*].{client_id, seconds_since_last_push,
 *                          sessions}. Auth: header must match a populated
 *                          slot once any slot is bound.
 *
 * A previously-registered POST /factory-reset was removed because it had
 * no auth on the LAN. Re-introduce alongside an auth scheme; the BOOT
 * long-press in factory_reset.c is the physical-presence reset path
 * until then.
 *
 * Call once after `IP_EVENT_STA_GOT_IP`; idempotent on repeat calls.
 * Captive-portal AP mode uses its own server in `provisioning.c`.
 */

void http_server_start(void);
