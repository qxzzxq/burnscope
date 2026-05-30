/*
 * CST9217 capacitive touch on the Waveshare ESP32-S3-Touch-AMOLED-1.75.
 *
 * Trimmed from the Waveshare vendor demo
 * (docs/ESP32-S3-Touch-AMOLED-1.75/examples/Arduino-v3.3.5/libraries/
 * SensorLib → src/touch/TouchDrvCST92xx.cpp). The board exposes the
 * controller on the same I2C0 bus as the QMI8658 IMU (SDA=GPIO15,
 * SCL=GPIO14, addr 0x5A). No INT handling: the controller is polled
 * from the LVGL indev callback in ui.c, matching the 1.43" profile.
 *
 * Report protocol (per the vendor driver's getPoint()):
 *   1. write the 16-bit read command 0xD000 (big-endian);
 *   2. read a 15-byte report (CST92XX_MAX_FINGER(2) * 5 + 5);
 *   3. write the read ACK {0xD0, 0x00, 0xAB} so the controller releases
 *      the report;
 *   4. verify report[6] == 0xAB, then numPoints = report[5] & 0x7F.
 *      Finger 0 lives in report[0..4]: the low nibble of report[0] is
 *      the event (0x06 = pressed), and x/y are packed across report[1..3].
 */

#include "touch.h"

#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c.h"
#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"  /* pdMS_TO_TICKS — direct include, not via i2c.h */
#include "freertos/task.h"

static const char *TAG = "touch";

#define TOUCH_I2C_PORT    I2C_NUM_0
#define TOUCH_PIN_SDA     GPIO_NUM_15   /* shared peripheral bus — see qmi8658.c */
#define TOUCH_PIN_SCL     GPIO_NUM_14
#define TOUCH_PIN_RST     GPIO_NUM_40   /* CST9217 reset (active-low) */
#define TOUCH_I2C_HZ      400000        /* matches qmi8658.c so the shared install agrees */
#define TOUCH_I2C_TIMEOUT pdMS_TO_TICKS(50)

#define CST9217_ADDR      0x5A          /* CST92XX_SLAVE_ADDRESS */
#define CST9217_READ_CMD_HI 0xD0        /* 0xD000 read command, big-endian */
#define CST9217_READ_CMD_LO 0x00
#define CST9217_ACK       0xAB

#define CST9217_MAX_FINGER 2
#define CST9217_REPORT_LEN (CST9217_MAX_FINGER * 5 + 5)  /* 15 bytes */
#define CST9217_EVT_DOWN  0x06          /* finger-present event in report[0] low nibble */

#define LCD_H_RES         466
#define LCD_V_RES         466

static bool s_initialised = false;

/* Install the I²C0 master on the 1.75"'s shared bus. Idempotent: the
 * QMI8658 may have brought the bus up first (and vice versa). A redundant
 * install is reported as ESP_ERR_INVALID_STATE on some IDF versions but
 * ESP_FAIL on v6.0.1 — either way the bus is up, so any install error is
 * treated as "already installed, proceed". touch_read's I2C transactions
 * are the real gate: a dead bus just yields no touches. Mirrors
 * qmi8658.c's `ensure_i2c_bus`; identical pins/speed so the order the two
 * run in doesn't matter. */
static esp_err_t ensure_i2c_bus(void)
{
    const i2c_config_t cfg = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = TOUCH_PIN_SDA,
        .scl_io_num = TOUCH_PIN_SCL,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = TOUCH_I2C_HZ,
    };
    esp_err_t err = i2c_param_config(TOUCH_I2C_PORT, &cfg);
    if (err != ESP_OK) {
        return err;
    }
    err = i2c_driver_install(TOUCH_I2C_PORT, cfg.mode, 0, 0, 0);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "i2c_driver_install: %s — assuming shared bus already up",
                 esp_err_to_name(err));
    }
    return ESP_OK;  /* reads are the real gate, not the install code */
}

/* Pulse the active-low reset line (vendor TouchDrvCST92xx::reset). */
static void reset_controller(void)
{
    const gpio_config_t rst_cfg = {
        .pin_bit_mask = 1ULL << TOUCH_PIN_RST,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    if (gpio_config(&rst_cfg) != ESP_OK) {
        return;  /* non-fatal: the controller is usually already out of reset */
    }
    gpio_set_level(TOUCH_PIN_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(TOUCH_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(50));  /* boot settle before first read */
}

void touch_init(void)
{
    if (s_initialised) {
        return;
    }

    reset_controller();

    esp_err_t err = ensure_i2c_bus();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "I2C bus install failed (%s) — touch disabled",
                 esp_err_to_name(err));
        return;
    }

    s_initialised = true;
    ESP_LOGI(TAG, "CST9217 ready (poll-only, no INT)");
}

bool touch_read(uint16_t *x, uint16_t *y)
{
    if (!s_initialised || x == NULL || y == NULL) {
        return false;
    }

    const uint8_t cmd[2] = { CST9217_READ_CMD_HI, CST9217_READ_CMD_LO };
    uint8_t report[CST9217_REPORT_LEN] = { 0 };
    if (i2c_master_write_read_device(TOUCH_I2C_PORT, CST9217_ADDR, cmd,
                                     sizeof cmd, report, sizeof report,
                                     TOUCH_I2C_TIMEOUT) != ESP_OK) {
        return false;
    }

    /* ACK the report so the controller can refresh it. Best-effort: a
     * failed ACK just means the next poll re-reads stale data. */
    const uint8_t ack[3] = { CST9217_READ_CMD_HI, CST9217_READ_CMD_LO,
                             CST9217_ACK };
    (void)i2c_master_write_to_device(TOUCH_I2C_PORT, CST9217_ADDR, ack,
                                     sizeof ack, TOUCH_I2C_TIMEOUT);

    if (report[6] != CST9217_ACK) {
        return false;  /* controller not ready / no valid report */
    }
    const uint8_t num_points = report[5] & 0x7F;
    if (num_points == 0 || num_points > CST9217_MAX_FINGER) {
        return false;
    }
    /* Finger 0 occupies report[0..4]; low nibble of report[0] is the
     * event (0x06 = down), x/y packed across report[1..3]. */
    if ((report[0] & 0x0F) != CST9217_EVT_DOWN) {
        return false;  /* a release/up report — not a press */
    }
    uint16_t raw_x = (uint16_t)((report[1] << 4) | (report[3] >> 4));
    uint16_t raw_y = (uint16_t)((report[2] << 4) | (report[3] & 0x0F));
    if (raw_x >= LCD_H_RES) raw_x = LCD_H_RES - 1;
    if (raw_y >= LCD_V_RES) raw_y = LCD_V_RES - 1;
    *x = raw_x;
    *y = raw_y;
    return true;
}
