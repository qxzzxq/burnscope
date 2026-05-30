#pragma once

#include <stdbool.h>
#include <stdint.h>

/*
 * qmi8658.h — minimal QMI8658C accelerometer driver for the Waveshare
 * 1.75" AMOLED board. Clone of the 1.43" profile's driver; only the
 * I²C bus pins differ. Gyro paths are dropped — only the accelerometer
 * is used (motion-wake source + orientation watcher).
 *
 * Bus: I²C0 on SDA=GPIO15 / SCL=GPIO14 (the 1.75" board's shared
 * peripheral bus), shared with the CST9217 touch driver (touch.c).
 * Both `qmi8658_init` and `touch_init` install it idempotently, so
 * either may run first.
 *
 * Settings: ±2 g range, ODR Qmi8658AccOdr_LowPower_21Hz, LPF disabled.
 * Sensitivity at ±2 g is 16384 LSB / g (i.e. 1 mg = 16.384 LSB).
 */

/**
 * Install the I²C0 bus (idempotent) and probe the WHO_AM_I register at
 * both candidate addresses (0x6A / 0x6B), expecting 0x05. On success,
 * configure Ctrl1 (auto-increment), Ctrl2 (range + ODR), Ctrl5 (LPF
 * disabled), and Ctrl7 (enable accel only). Returns true on full
 * success; false (non-fatal — does not abort startup) on any I²C or
 * signature failure, leaving the adapter free to continue without
 * motion wake / orientation.
 */
bool qmi8658_init(void);

/**
 * Read the latest accelerometer sample. Writes per-axis values in
 * milli-g into out[0..2] (X, Y, Z). Returns false if the chip was
 * never initialised or if the I²C read fails — out is unmodified on
 * failure.
 */
bool qmi8658_read_accel_mg(int16_t out[3]);
