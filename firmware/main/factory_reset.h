#pragma once

/*
 * factory_reset.h — long-press monitor on GPIO0 (BOOT button on the CYD).
 *
 * Holding the button continuously LOW for ≥ 5 s erases the WiFi
 * credentials from NVS and reboots, so the captive portal comes back up
 * on next boot. Covers FR-1.7 / TC-NVS-102.
 *
 * Spawn the task once at boot from both the STA and AP paths — the user
 * should be able to re-provision regardless of current mode.
 */

void factory_reset_start(void);
