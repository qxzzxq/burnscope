#pragma once

#include "lvgl.h"

/*
 * orientation — Phase 3 of the OLED burn-in mitigation FSD.
 *
 * Spawns a low-priority task that samples the QMI8658 accelerometer
 * (already brought up by burn_idle_adapter), classifies which of the
 * ±X / ±Y axes carries the dominant gravity component, and rotates
 * the LVGL display in 90° quadrants. Hysteresis (FR-1.3) prevents
 * flapping at the 45° boundary; a 500 ms debounce (FR-1.4) absorbs
 * quick flips through an intermediate quadrant.
 *
 * Idempotent. No-op if the IMU never came up, or if
 * CONFIG_BURNSCOPE_AMOLED_ORIENTATION_AUTO is disabled — in the
 * latter case the burn-in motion-wake path (FR-3.1) still runs,
 * matching IMU-ROT-004 in § 8.3 of the FSD.
 *
 * Must be called after both `amoled_sh8601_driver_init()` (so the
 * supplied display handle is non-NULL) and `burn_idle_adapter_start()`
 * (so `qmi8658_init` has run).
 */
void orientation_start(lv_display_t *disp);
