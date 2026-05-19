#pragma once

/*
 * provisioning.h — first-boot WiFi setup over an open SoftAP.
 *
 * `provisioning_start` performs:
 *   1. Computes the AP SSID `BURNSCOPE-<XXXX>` from the device MAC
 *      (FR-1.3).
 *   2. Brings up an open AP via `wifi_start_ap`.
 *   3. Spawns the captive-DNS responder so phone OSes auto-load the
 *      portal.
 *   4. Starts an HTTP server with the portal form, scan endpoint, and
 *      the iOS/Android probe handlers.
 *
 * On `POST /provision` the form handler writes creds to NVS and triggers
 * `esp_restart()`. There is no graceful shutdown — power-cycling the
 * radios at boundary is the simplest way to drop the AP cleanly.
 */

void provisioning_start(void);
