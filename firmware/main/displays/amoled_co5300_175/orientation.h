#pragma once

#include "lvgl.h"

/*
 * orientation — IMU-driven auto-rotate for the Waveshare 1.75" AMOLED.
 * Clone of the 1.43" profile's orientation watcher.
 *
 * Spawns a low-priority task that samples the QMI8658 accelerometer
 * (already brought up by burn_idle_adapter), classifies which of the
 * ±X / ±Y axes carries the dominant gravity component, and rotates
 * the LVGL display in 90° quadrants. Hysteresis prevents flapping at
 * the 45° boundary; a 500 ms debounce absorbs quick flips.
 *
 * Idempotent. No-op if the IMU never came up, or if
 * CONFIG_BURNSCOPE_AMOLED_ORIENTATION_AUTO is disabled — in the latter
 * case the burn-in motion-wake path still runs.
 *
 * Must be called after both `amoled_co5300_175_driver_init()` (so the
 * supplied display handle is non-NULL) and `burn_idle_adapter_start()`
 * (so `qmi8658_init` has run).
 */
void orientation_start(lv_display_t *disp);
