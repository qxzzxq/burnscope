#pragma once

#include <stdbool.h>
#include <stdint.h>

/*
 * touch.h — FT3168 capacitive touch driver for the Waveshare 1.43"
 * AMOLED board. Polled (no INT line is routed on this board — the
 * vendor LVGL demo reads the controller from inside an LVGL input-device
 * callback at ~30 Hz).
 *
 * Owns the I2C0 master install (SDA=GPIO 47, SCL=GPIO 48), which is the
 * same bus the QMI8658 IMU uses; PR-2's IMU port attaches to the bus
 * without re-installing.
 */

/**
 * Configure the I2C0 master, install the bus driver, and switch the
 * FT3168 to its active sensing mode. Idempotent — the I2C driver is
 * installed at most once.
 */
void touch_init(void);

/**
 * Read the currently-pressed coordinate. Returns true if a touch is
 * present and writes the LCD-space (0..LCD_H_RES-1, 0..LCD_V_RES-1)
 * coordinates into *x and *y. Returns false (and leaves the outputs
 * unmodified) when no contact is detected.
 *
 * Caller is responsible for any rotation/inversion the LVGL display
 * uses; this function returns raw FT3168 coordinates clamped to the
 * panel bounds. Safe to call at LVGL's input-device polling cadence.
 */
bool touch_read(uint16_t *x, uint16_t *y);
