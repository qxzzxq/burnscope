#pragma once

/*
 * watchdog.h — Phase 1 wrapper around ESP-IDF Task Watchdog (TWDT).
 *
 * The TWDT itself is initialised by IDF at boot (CONFIG_ESP_TASK_WDT_INIT)
 * with a 10 s timeout. This module spawns a 2 KiB heartbeat task that
 * subscribes itself and resets the WDT every second so the subsystem can
 * be exercised end-to-end (TC-WDT-100) before other long-lived tasks
 * (renderer, snapshot store, …) come online in later phases.
 *
 * Define BURNSCOPE_WDT_INJECT_HANG at compile time to make the heartbeat
 * task stop feeding the WDT after 5 s — used to verify the device reboots
 * (`rst:0xc`) under TC-WDT-100. Off by default.
 */

/**
 * Spawn the heartbeat task. Returns immediately. Idempotent.
 */
void watchdog_start(void);
