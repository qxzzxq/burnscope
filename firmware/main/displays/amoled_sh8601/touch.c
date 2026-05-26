/*
 * FT3168 capacitive touch on the Waveshare 1.43" AMOLED board.
 *
 * Trim of docs/ESP32-S3-AMOLED-1.43-Demo/08_LVGL_SDIMG/components/
 * touch_bsp/touch_bsp.c. The board exposes the controller on the same
 * I2C bus as the QMI8658 IMU (SDA=47, SCL=48). No INT pin is routed,
 * so the controller is polled — the LVGL indev callback (in ui.c)
 * drives the cadence.
 *
 * Bus speed is 300 kHz, not the 600 kHz of the vendor demo, so the
 * QMI8658 (which the vendor demo runs at 300 kHz) can share the same
 * driver install without re-init.
 */

#include "touch.h"

#include <string.h>

#include "driver/i2c.h"
#include "esp_err.h"
#include "esp_log.h"

static const char *TAG = "touch";

#define TOUCH_I2C_PORT    I2C_NUM_0
#define TOUCH_PIN_SDA     GPIO_NUM_47
#define TOUCH_PIN_SCL     GPIO_NUM_48
#define TOUCH_I2C_HZ      300000
#define TOUCH_I2C_TIMEOUT pdMS_TO_TICKS(50)

#define FT3168_ADDR       0x38
#define FT3168_REG_MODE   0x00  /* Operating mode select */
#define FT3168_REG_TD     0x02  /* Touch points present (0/1/2) */
#define FT3168_REG_COORD  0x03  /* X high (0xF0=event, 0x0F=high nibble) */

#define LCD_H_RES         466
#define LCD_V_RES         466

static bool s_initialised = false;

static esp_err_t i2c_write_reg(uint8_t reg, uint8_t value)
{
    uint8_t buf[2] = { reg, value };
    return i2c_master_write_to_device(TOUCH_I2C_PORT, FT3168_ADDR, buf,
                                      sizeof buf, TOUCH_I2C_TIMEOUT);
}

static esp_err_t i2c_read_regs(uint8_t reg, uint8_t *out, size_t len)
{
    return i2c_master_write_read_device(TOUCH_I2C_PORT, FT3168_ADDR, &reg, 1,
                                        out, len, TOUCH_I2C_TIMEOUT);
}

void touch_init(void)
{
    if (s_initialised) {
        return;
    }

    const i2c_config_t cfg = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = TOUCH_PIN_SDA,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_io_num = TOUCH_PIN_SCL,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = TOUCH_I2C_HZ,
    };
    ESP_ERROR_CHECK(i2c_param_config(TOUCH_I2C_PORT, &cfg));
    ESP_ERROR_CHECK(i2c_driver_install(TOUCH_I2C_PORT, cfg.mode, 0, 0, 0));

    /* Mode register 0x00 = 0x00 → Normal sensing mode. Vendor demo
     * pattern; the FT3168 boots into this mode already, the write is
     * defensive against alternative POR states. */
    esp_err_t err = i2c_write_reg(FT3168_REG_MODE, 0x00);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "FT3168 mode-select failed: %s — proceeding anyway",
                 esp_err_to_name(err));
    }

    s_initialised = true;
    ESP_LOGI(TAG, "FT3168 ready (poll-only, no INT)");
}

bool touch_read(uint16_t *x, uint16_t *y)
{
    if (!s_initialised || x == NULL || y == NULL) {
        return false;
    }

    uint8_t td = 0;
    if (i2c_read_regs(FT3168_REG_TD, &td, 1) != ESP_OK || td == 0) {
        return false;
    }

    uint8_t buf[4] = { 0 };
    if (i2c_read_regs(FT3168_REG_COORD, buf, sizeof buf) != ESP_OK) {
        return false;
    }

    /* High nibble of buf[0] is the event flag; low nibble is X bits 11..8.
     * Same packing for Y in buf[2]. Vendor demo masks 0x0F. */
    uint16_t raw_x = (uint16_t)(((buf[0] & 0x0F) << 8) | buf[1]);
    uint16_t raw_y = (uint16_t)(((buf[2] & 0x0F) << 8) | buf[3]);
    if (raw_x >= LCD_H_RES) raw_x = LCD_H_RES - 1;
    if (raw_y >= LCD_V_RES) raw_y = LCD_V_RES - 1;
    *x = raw_x;
    *y = raw_y;
    return true;
}
