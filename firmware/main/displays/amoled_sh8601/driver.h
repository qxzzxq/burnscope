#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "lvgl.h"

/*
 * driver.h — internal helper exposed by the amoled_sh8601 profile's
 * driver.c to its own ui.c and the burn-in idle adapter. File-private to
 * this profile directory; not part of the public profile interface.
 */

/**
 * Bring up the 466×466 Waveshare 1.43" round AMOLED (SH8601 or CO5300
 * silicon — same QSPI protocol) and register the LVGL display. Returns
 * the LVGL display handle, or NULL on failure. Idempotent — call once
 * during `display_profile_init`.
 */
lv_display_t *amoled_sh8601_driver_init(void);

/**
 * Write the SH8601 / CO5300 brightness register (0x51) at runtime.
 *
 * `pct` is clamped to [0, 100] and scaled to the 8-bit register range
 * (`pct * 255 / 100`). No-op if the panel has not been initialised. Safe
 * to call from any task; the underlying ESP-LCD panel-IO driver
 * serialises QSPI traffic internally.
 */
void amoled_sh8601_set_brightness_pct(uint8_t pct);

/**
 * Toggle DISPON / DISPOFF (MIPI DCS 0x28 / 0x29) so the panel stops
 * emitting while leaving the framebuffer pipeline intact. No-op if the
 * panel has not been initialised.
 */
void amoled_sh8601_set_display_on(bool on);
