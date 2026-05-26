#pragma once

#include <stdbool.h>
#include <stdint.h>

/*
 * qmi8658.h — minimal QMI8658C accelerometer driver for the Waveshare
 * 1.43" AMOLED board. Trimmed from the vendor demo at
 * docs/ESP32-S3-AMOLED-1.43-Demo/03_I2C_QMI8658/components/qmi8658c/
 * (Waveshare + Espressif, Apache-2.0). Gyro paths are dropped (FR-1.5
 * in docs/fsd/oled-burn-in-mitigation-fsd.md — only the accelerometer
 * is used as a motion-wake source for the OLED burn-in adapter).
 *
 * Bus: I²C0, shared with FT3168 touch. touch_init() is responsible for
 * installing the I²C driver; this module only configures the chip.
 *
 * Settings: ±2 g range, ODR Qmi8658AccOdr_LowPower_21Hz, LPF disabled.
 * Sensitivity at ±2 g is 16384 LSB / g (i.e. 1 mg = 16.384 LSB).
 */

/**
 * Probe the WHO_AM_I register at I²C address 0x6A; expect 0x05. On
 * success, configure Ctrl1 (auto-increment for multi-byte reads),
 * Ctrl2 (range + ODR), and Ctrl7 (enable accel only). Returns true on
 * full success. Returns false (without aborting the build) on any I²C
 * or signature failure, leaving the adapter free to continue without
 * motion wake.
 */
bool qmi8658_init(void);

/**
 * Read the latest accelerometer sample. Writes per-axis values in
 * milli-g into out[0..2] (X, Y, Z). Returns false if the chip was
 * never initialised or if the I²C read fails — out is unmodified on
 * failure.
 */
bool qmi8658_read_accel_mg(int16_t out[3]);
