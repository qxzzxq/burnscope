#pragma once

#include <stdbool.h>
#include <stdint.h>

/*
 * touch.h — CST9217 capacitive touch driver for the Waveshare
 * ESP32-S3-Touch-AMOLED-1.75 board.
 *
 * Minimal, poll-only: just enough to detect a finger-down for the
 * burn-in adapter's touch-wake source and feed LVGL pointer coordinates.
 * Mirrors the 1.43" profile's FT3168 `touch.c` in shape, but speaks the
 * CST9217 report protocol from the Waveshare vendor demo
 * (docs/ESP32-S3-Touch-AMOLED-1.75 → SensorLib `TouchDrvCST92xx`):
 * write the 0xD000 read command, read the report, then ACK it.
 *
 * Shares the I2C0 master bus with the QMI8658 IMU (SDA=GPIO15,
 * SCL=GPIO14). The install is idempotent — whichever of `touch_init`
 * or `qmi8658_init` runs first installs the driver; the other attaches
 * without re-installing (a second install returns ESP_ERR_INVALID_STATE,
 * which both accept). The INT line (GPIO11) is not used; the controller
 * is polled from the LVGL input-device callback in ui.c, matching the
 * 1.43" profile's cadence.
 */

/**
 * Pulse the CST9217 reset line, ensure the shared I2C0 bus is installed,
 * and mark the controller ready. Idempotent — safe to call once at boot
 * regardless of whether the IMU brought the bus up first.
 */
void touch_init(void);

/**
 * Read the currently-pressed coordinate. Returns true if a finger is
 * down and writes the LCD-space (0..LCD_H_RES-1, 0..LCD_V_RES-1)
 * coordinates into *x and *y; returns false (leaving the outputs
 * unmodified) when no contact is present or the report is malformed.
 *
 * Returns raw controller coordinates clamped to the panel bounds; the
 * caller handles any rotation/inversion the LVGL display applies. Safe
 * to call at LVGL's input-device polling cadence.
 */
bool touch_read(uint16_t *x, uint16_t *y);
