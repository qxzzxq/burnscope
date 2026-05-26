/*
 * QMI8658C accelerometer driver — minimal, accel-only trim of the
 * Waveshare vendor demo (`docs/ESP32-S3-AMOLED-1.43-Demo/
 * 03_I2C_QMI8658/components/qmi8658c/`). Apache-2.0 attribution to
 * Waveshare and Espressif preserved by inheritance — this file
 * carries only the register sequence we need:
 *
 *   - Probe WHO_AM_I (0x00) → 0x05.
 *   - Ctrl1 (0x02) ← 0x60: SPI auto-increment + big-endian-disable.
 *     Essential for the six-byte block read of the accel data
 *     registers; without it the bus would auto-stop after the first
 *     byte.
 *   - Ctrl2 (0x03) ← (Qmi8658AccRange_2g | Qmi8658AccOdr_LowPower_21Hz)
 *     = 0x0D. ±2 g range gives 16384 LSB/g sensitivity.
 *   - Ctrl5 (0x06) ← 0x00: LPF disabled (we read raw and software-
 *     filter via per-axis delta-magnitude in the burn_idle adapter).
 *   - Ctrl7 (0x08) ← 0x01: accel enabled, gyro disabled.
 *
 * Read accel: 6 bytes starting at Ax_L (0x35). Each int16_t is LSB-
 * first (per Ctrl1=0x60). mg = raw * 1000 / 16384.
 *
 * Bus install: shared with FT3168 touch. touch_init() owns the I²C0
 * driver_install; this module only does register reads/writes.
 */

#include "qmi8658.h"

#include "driver/i2c.h"
#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"  /* pdMS_TO_TICKS — direct include */

static const char *TAG = "qmi8658";

#define QMI8658_I2C_PORT      I2C_NUM_0
#define QMI8658_ADDR_L        0x6A   /* SA0 pin low */
#define QMI8658_ADDR_H        0x6B   /* SA0 pin high — Waveshare 1.43 uses this on some board revs */
#define QMI8658_I2C_TIMEOUT   pdMS_TO_TICKS(50)

#define REG_WHO_AM_I          0x00   /* expected 0x05 */
#define REG_CTRL1             0x02
#define REG_CTRL2             0x03
#define REG_CTRL5             0x06
#define REG_CTRL7             0x08
#define REG_ACCEL_DATA_START  0x35   /* Ax_L; six bytes through Az_H */

#define CTRL1_AUTO_INCREMENT  0x60
#define CTRL2_2G_LP_21HZ      0x0D   /* Qmi8658AccRange_2g | Qmi8658AccOdr_LowPower_21Hz */
#define CTRL5_LPF_DISABLED    0x00
#define CTRL7_ACC_ONLY        0x01

#define ACCEL_LSB_PER_G       16384  /* ±2 g range sensitivity */

static bool    s_initialised = false;
static uint8_t s_addr         = QMI8658_ADDR_L;

static esp_err_t write_reg(uint8_t reg, uint8_t value)
{
    const uint8_t buf[2] = { reg, value };
    return i2c_master_write_to_device(QMI8658_I2C_PORT, s_addr, buf,
                                      sizeof buf, QMI8658_I2C_TIMEOUT);
}

static esp_err_t read_regs(uint8_t reg, uint8_t *out, size_t len)
{
    return i2c_master_write_read_device(QMI8658_I2C_PORT, s_addr,
                                        &reg, 1, out, len,
                                        QMI8658_I2C_TIMEOUT);
}

bool qmi8658_init(void)
{
    /* Probe both QMI8658 slave addresses — the vendor demo does the
     * same, since the SA0 pin's wiring varies between Waveshare board
     * revisions. The first one that returns WHO_AM_I=0x05 wins. */
    const uint8_t candidates[] = { QMI8658_ADDR_L, QMI8658_ADDR_H };
    uint8_t who = 0;
    bool found = false;
    for (size_t i = 0; i < sizeof candidates / sizeof candidates[0]; ++i) {
        s_addr = candidates[i];
        esp_err_t err = read_regs(REG_WHO_AM_I, &who, 1);
        if (err == ESP_OK && who == 0x05) {
            found = true;
            break;
        }
        ESP_LOGD(TAG, "probe @0x%02x: err=%s who=0x%02x",
                 s_addr, esp_err_to_name(err), who);
    }
    if (!found) {
        ESP_LOGW(TAG, "no QMI8658 at 0x6A or 0x6B — IMU disabled");
        return false;
    }

    /* Order matches the vendor demo: configure regs while accel is
     * disabled (Ctrl7=0), then enable. Failures here are non-fatal —
     * the adapter falls back to "no motion wake" and logs. */
    esp_err_t e1 = write_reg(REG_CTRL7, 0x00);
    esp_err_t e2 = write_reg(REG_CTRL1, CTRL1_AUTO_INCREMENT);
    esp_err_t e3 = write_reg(REG_CTRL2, CTRL2_2G_LP_21HZ);
    esp_err_t e4 = write_reg(REG_CTRL5, CTRL5_LPF_DISABLED);
    esp_err_t e5 = write_reg(REG_CTRL7, CTRL7_ACC_ONLY);
    if (e1 || e2 || e3 || e4 || e5) {
        ESP_LOGW(TAG,
                 "config writes failed (%s/%s/%s/%s/%s) — IMU disabled",
                 esp_err_to_name(e1), esp_err_to_name(e2),
                 esp_err_to_name(e3), esp_err_to_name(e4),
                 esp_err_to_name(e5));
        return false;
    }

    s_initialised = true;
    ESP_LOGI(TAG, "ready @0x%02x (WHO_AM_I=0x05, ±2 g, ODR LowPower_21Hz)",
             s_addr);
    return true;
}

bool qmi8658_read_accel_mg(int16_t out[3])
{
    if (!s_initialised || out == NULL) {
        return false;
    }
    uint8_t buf[6] = { 0 };
    if (read_regs(REG_ACCEL_DATA_START, buf, sizeof buf) != ESP_OK) {
        return false;
    }
    /* int16_t LSB-first per Ctrl1=0x60. Cast the intermediate to
     * int32 because `raw * 1000` (range ±2,000,000) overflows int16
     * before the divide by ACCEL_LSB_PER_G (16384 LSB/g sensitivity)
     * brings it back into the mg range. */
    for (int axis = 0; axis < 3; ++axis) {
        const int16_t raw = (int16_t)((uint16_t)buf[axis * 2] |
                                      ((uint16_t)buf[axis * 2 + 1] << 8));
        out[axis] = (int16_t)(((int32_t)raw * 1000) / ACCEL_LSB_PER_G);
    }
    return true;
}
