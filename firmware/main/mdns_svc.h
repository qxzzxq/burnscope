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
 * `version=<fw>`, `paired_claude=<0|1>`, `paired_codex=<0|1>` TXT records.
 * `paired_<agent>` reflects the current NVS pairing slot for that agent
 * at registration time. No-op after the first successful call.
 */
void mdns_svc_start(void);

/**
 * Update the `paired_<agent>` TXT item to reflect the current NVS state.
 * Call after every transition that adds or removes an `<agent>` slot
 * binding (TOFU claim in `authorize_summary`, factory-reset path). No-op
 * if mDNS hasn't been started yet, or if `agent` is not one of "claude"
 * or "codex".
 */
void mdns_svc_refresh_paired(const char *agent);
