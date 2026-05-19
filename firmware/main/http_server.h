#pragma once

/*
 * http_server.h — Phase 1 HTTP skeleton.
 *
 * Two routes, both on port 80:
 *   POST /summary  — drains the body (up to 16 KiB) and returns 204.
 *                    Phase 2 parses the AgentSnapshot.
 *   GET  /health   — returns minimal JSON: firmware_version, uptime_s,
 *                    free_heap_b. `agents.*` block is Phase 2.
 *
 * Call once after `IP_EVENT_STA_GOT_IP`; idempotent on repeat calls.
 */

/**
 * Bring up the HTTP server. No-op after the first successful start.
 */
void http_server_start(void);
