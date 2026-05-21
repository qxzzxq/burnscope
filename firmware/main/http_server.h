#pragma once

/*
 * http_server.h — production HTTP routes (STA mode only).
 *
 * Three endpoints on port 80:
 *   POST /summary        — parse one AgentSnapshot per `docs/wire-format.md`,
 *                          put into the snapshot store, respond 204.
 *                          400 on bad JSON / schema / range. 413 over 16 KiB.
 *   GET  /health         — JSON: firmware_version, uptime_s, free_heap_b,
 *                          agents[*].seconds_since_last_push.
 *   POST /factory-reset  — wipe creds, reply 202, reboot.
 *
 * Call once after `IP_EVENT_STA_GOT_IP`; idempotent on repeat calls.
 * Captive-portal AP mode uses its own server in `provisioning.c`.
 */

void http_server_start(void);
