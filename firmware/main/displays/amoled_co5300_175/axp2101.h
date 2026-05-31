#pragma once

#include <stdbool.h>
#include <stdint.h>

/*
 * axp2101 — battery / power telemetry for the Waveshare 1.75" AMOLED
 * board's AXP2101 PMU. Read-only: this profile does not drive any power
 * rails (the panel rail is on by the board's power-on defaults — see
 * driver.c). We only read the on-chip fuel gauge and charger status so
 * the UI can show a battery indicator when a pack is attached.
 *
 * The AXP2101 sits at I²C address 0x34 on the same I²C0 bus as the
 * CST9217 touch controller and the QMI8658 IMU (SDA=GPIO15, SCL=GPIO14);
 * `axp2101_init` installs that bus idempotently, exactly like touch.c /
 * qmi8658.c. Register semantics are mirrored from the Waveshare vendor
 * demo `ESP-IDF-v5.5/01_AXP2101` (XPowersLib).
 */

/** Snapshot of AXP2101 power state, filled by `axp2101_read`. */
typedef struct {
    bool     present;      /* battery physically connected (STATUS1 bit 3) */
    int8_t   percent;      /* fuel-gauge 0..100 %, -1 when absent/unknown  */
    uint16_t millivolts;   /* battery voltage in mV, 0 when absent         */
    bool     charging;     /* charger in the constant-current/voltage state */
    bool     vbus;         /* external 5 V (USB) present (STATUS1 bit 5)    */
} axp2101_status_t;

/**
 * Probe and configure the AXP2101 on the shared I²C0 bus.
 *
 * Verifies the chip id (IC_TYPE 0x03 == 0x4A), enables the battery-voltage
 * ADC, and disables the TS-pin (NTC) measurement so a thermistor-less pack
 * charges normally — without this the AXP2101 mis-handles charging (per the
 * vendor demo's note). Returns false if no AXP2101 answers at 0x34, in
 * which case the caller should skip the battery UI entirely. Idempotent;
 * safe to call after touch_init / qmi8658_init have already installed the bus.
 */
bool axp2101_init(void);

/**
 * Read a fresh power snapshot into `*out`. Returns false on I²C error or
 * if `axp2101_init` has not succeeded. When no battery is connected,
 * returns true with `present=false`, `percent=-1`, `millivolts=0`.
 */
bool axp2101_read(axp2101_status_t *out);
