#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "lvgl.h"

/*
 * driver.h — internal helper exposed by the amoled_co5300_175 profile's
 * driver.c to its own ui.c and the burn-in idle adapter. File-private to
 * this profile directory; not part of the public profile interface.
 *
 * This profile targets the Waveshare ESP32-S3-Touch-AMOLED-1.75" board:
 * a 466×466 round AMOLED on a CO5300 controller over QSPI. It is a clone
 * of the amoled_sh8601 (1.43") profile — same pixel grid and UI, but
 * CO5300-only (no runtime SH8601/CO5300 detection), different GPIO pins,
 * and no IMU/touch (see sources.cmake).
 */

/**
 * Bring up the 466×466 Waveshare 1.75" round AMOLED (CO5300 silicon over
 * QSPI) and register the LVGL display. Returns the LVGL display handle,
 * or NULL on failure. Idempotent — call once during `display_profile_init`.
 *
 * Locking: must run before any other function in this header is called.
 * Initialises `lvgl_port` and the QSPI panel-IO; once it returns, the
 * other functions below can be called from any task and will internally
 * acquire `lvgl_port_lock` to coordinate with the LVGL flush pipeline.
 */
lv_display_t *amoled_co5300_175_driver_init(void);

/**
 * Write the CO5300 brightness register (0x51) at runtime.
 *
 * `pct` is clamped to [0, 100] and scaled to the 8-bit register range
 * (`pct * 255 / 100`). No-op if the panel has not been initialised.
 *
 * Locking: internally acquires `lvgl_port_lock` for the duration of the
 * `esp_lcd_panel_io_tx_param` call. `lvgl_port_lock` is a FreeRTOS
 * recursive mutex, so a caller already holding the lock can call this
 * safely; the second take just increments the recursion count.
 *
 * Why this lock is needed even though `esp_lcd_panel_io_spi` has its own
 * internal mutex: the panel-IO mutex only serialises QSPI bytes on the
 * wire. Its transaction-done notification is associated per-bus, so a
 * brightness write concurrent with an in-flight LVGL pixel flush can
 * cause the flush-done semaphore to be lost — wedging LVGL and, on the
 * next call here, this function as well.
 */
void amoled_co5300_175_set_brightness_pct(uint8_t pct);

/**
 * Toggle DISPON / DISPOFF (MIPI DCS 0x28 / 0x29) so the panel stops
 * emitting while leaving the framebuffer pipeline intact. No-op if the
 * panel has not been initialised.
 *
 * Locking: same contract as `amoled_co5300_175_set_brightness_pct` —
 * holds `lvgl_port_lock` for the underlying `esp_lcd_panel_disp_on_off`
 * call. Safe to call while already holding the lock (recursive mutex).
 */
void amoled_co5300_175_set_display_on(bool on);
