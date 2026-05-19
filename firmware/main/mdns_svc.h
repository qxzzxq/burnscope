#pragma once

/*
 * mdns_svc.h — advertise `_burnscope._tcp.local` on port 80.
 *
 * Sets the device hostname to `burnscope-XXXX` where XXXX is the last
 * four hex digits of the WiFi STA MAC (lowercased) — multiple devices on
 * one LAN avoid collisions (FR-1.3 + § 6.1.4).
 *
 * Call once after the first `IP_EVENT_STA_GOT_IP`. Idempotent on repeat
 * calls (subsequent calls are no-ops).
 */

/**
 * Start the mDNS responder and advertise `_burnscope._tcp` with the
 * `version=<fw>` TXT record. No-op after the first successful call.
 */
void mdns_svc_start(void);
