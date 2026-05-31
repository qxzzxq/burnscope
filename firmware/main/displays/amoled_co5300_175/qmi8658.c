/*
 * QMI8658C accelerometer driver for the Waveshare 1.75" AMOLED board.
 * Clone of the 1.43" profile's qmi8658.c with two differences:
 *   - I²C bus pins: SDA=GPIO15 / SCL=GPIO14 (vs 47/48 on the 1.43").
 *   - The I²C0 bus is shared with the CST9217 touch driver (touch.c).
 *     Both install it idempotently — whichever of `qmi8658_init` or
 *     `touch_init` runs first wins; the other's redundant install is
 *     tolerated (see ensure_i2c_bus) and the WHO_AM_I probe gates health.
 *
 * Register map / settings are identical to the 1.43" (±2 g, ODR
 * LowPower_21Hz, LPF off). Confirmed against the 1.75" vendor demo
 * `04_LVGL_QMI8658_ui` (addr QMI8658_L_SLAVE_ADDRESS = 0x6A on the
 * shared SDA15/SCL14 bus).
 */

#include "qmi8658.h"

#include "driver/i2c.h"
#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"  /* pdMS_TO_TICKS — direct include */

static const char *TAG = "qmi8658";

#define QMI8658_I2C_PORT      I2C_NUM_0
#define QMI8658_I2C_SDA       15     /* IIC_SDA — shared peripheral bus */
#define QMI8658_I2C_SCL       14     /* IIC_SCL */
#define QMI8658_I2C_HZ        400000
#define QMI8658_ADDR_L        0x6A   /* SA0 pin low (vendor demo default) */
#define QMI8658_ADDR_H        0x6B   /* SA0 pin high — covered for board-rev variance */
#define QMI8658_I2C_TIMEOUT   pdMS_TO_TICKS(50)

#define REG_WHO_AM_I          0x00   /* expected 0x05 */
#define REG_CTRL1             0x02
#define REG_CTRL2             0x03
#define REG_CTRL5             0x06
#define REG_CTRL7             0x08
#define REG_ACCEL_DATA_START  0x35   /* Ax_L; six bytes through Az_H */

#define CTRL1_AUTO_INCREMENT  0x60
/* Normal-mode (continuous) ODR — NOT the duty-cycled LowPower modes the
 * 1.43" profile uses. On the 1.75" board the LowPower_21Hz mode produced
 * a large (~0.8 g) DC offset on the accel X axis that broke orientation;
 * normal mode reads true gravity. ±2 g @ 250 Hz is ample for the 10 Hz
 * orientation watcher and motion sampler (both 10 Hz), and matches the spirit of
 * the vendor demo (which runs normal ODR, not LowPower). */
#define CTRL2_2G_NORM_250HZ   0x05   /* aFS=±2g (000) | aODR=250 Hz (0x5) */
/* Accel low-pass filter, mode 0 (≈2.66% of ODR), enabled — matches the
 * vendor demo (LPF_MODE_0) and smooths motion/orientation classification. */
#define CTRL5_LPF_MODE0_EN    0x01   /* aLPF_MODE=00 (<<1) | aLPF_EN=1 */
#define CTRL7_ACC_ONLY        0x01

#define ACCEL_LSB_PER_G       16384  /* ±2 g range sensitivity */

static bool    s_initialised = false;
static uint8_t s_addr         = QMI8658_ADDR_L;

/* Install the I²C0 master on the 1.75"'s shared bus. Idempotent: the bus
 * is shared with touch.c (CST9217), and whichever of the two runs first
 * installs it. A redundant install is reported as ESP_ERR_INVALID_STATE
 * on some IDF versions but ESP_FAIL on v6.0.1 — either way the bus is up,
 * so any install error is treated as "already installed, proceed". The
 * WHO_AM_I probe in qmi8658_init is the real bus-health gate: a genuinely
 * dead bus fails there and disables the IMU. */
static esp_err_t ensure_i2c_bus(void)
{
    const i2c_config_t cfg = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = QMI8658_I2C_SDA,
        .scl_io_num = QMI8658_I2C_SCL,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = QMI8658_I2C_HZ,
    };
    esp_err_t err = i2c_param_config(QMI8658_I2C_PORT, &cfg);
    if (err != ESP_OK) {
        return err;
    }
    err = i2c_driver_install(QMI8658_I2C_PORT, cfg.mode, 0, 0, 0);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "i2c_driver_install: %s — assuming shared bus already up",
                 esp_err_to_name(err));
    }
    return ESP_OK;  /* reads/probe are the real gate, not the install code */
}

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
    esp_err_t bus = ensure_i2c_bus();
    if (bus != ESP_OK) {
        ESP_LOGW(TAG, "I2C bus install failed (%s) — IMU disabled",
                 esp_err_to_name(bus));
        return false;
    }

    /* Probe both QMI8658 slave addresses — the vendor demo uses 0x6A,
     * but the SA0 pin's wiring can vary, so we also try 0x6B. The first
     * that returns WHO_AM_I=0x05 wins. */
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
    esp_err_t e3 = write_reg(REG_CTRL2, CTRL2_2G_NORM_250HZ);
    esp_err_t e4 = write_reg(REG_CTRL5, CTRL5_LPF_MODE0_EN);
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
    ESP_LOGI(TAG, "ready @0x%02x (WHO_AM_I=0x05, ±2 g, ODR 250Hz normal, LPF on)",
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
    /* int16_t LSB-first per Ctrl1=0x60. Cast the intermediate to int32
     * because `raw * 1000` (range ±2,000,000) overflows int16 before the
     * divide by ACCEL_LSB_PER_G (16384 LSB/g) brings it into mg range. */
    for (int axis = 0; axis < 3; ++axis) {
        const int16_t raw = (int16_t)((uint16_t)buf[axis * 2] |
                                      ((uint16_t)buf[axis * 2 + 1] << 8));
        out[axis] = (int16_t)(((int32_t)raw * 1000) / ACCEL_LSB_PER_G);
    }
    return true;
}
