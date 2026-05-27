#pragma once

#include <stddef.h>

/*
 * device_id.h — stable per-device identifier.
 *
 * Same value as the mDNS hostname (`burnscope-XXXX` where XXXX is the
 * last two hex bytes of the WiFi STA MAC, lowercased). Exposed as a
 * shared accessor so the mDNS responder and the `/health` JSON emitter
 * cannot drift from each other — the client side trusts that they
 * match for identity verification on duplicate-host conflicts.
 */

/* `burnscope-` (10) + 4 hex chars (4) + NUL = 15; round up to 16. */
#define DEVICE_ID_MAX 16

/*
 * Return a pointer to the cached device_id string. First call computes
 * the value from the WiFi STA MAC; subsequent calls return the cached
 * value in O(1). The returned pointer is stable for the lifetime of
 * the process.
 *
 * Panics (ESP_ERROR_CHECK) on a MAC read failure — matching the
 * existing hostname-derivation path in mdns_svc.c. There is no
 * graceful degradation: without a stable device_id the firmware cannot
 * advertise itself on mDNS, so a panic is the right failure mode.
 */
const char *device_id_str(void);
