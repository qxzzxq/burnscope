/*
 * AXP2101 PMU battery telemetry for the Waveshare 1.75" AMOLED board.
 *
 * Read-only driver: we never touch the power rails (the board powers the
 * panel from its defaults — see driver.c). The AXP2101 shares the I²C0
 * bus with the CST9217 touch controller and the QMI8658 IMU on
 * SDA=GPIO15 / SCL=GPIO14, so the bus install here is idempotent in the
 * exact same way qmi8658.c / touch.c are: whichever runs first installs
 * the driver, the others tolerate the redundant install, and the real
 * health gate is the chip-id probe.
 *
 * Register addresses and bit semantics are mirrored from the vendor demo
 * `ESP-IDF-v5.5/01_AXP2101` and its XPowersLib (XPowersAXP2101.tpp):
 *   - STATUS1 (0x00): bit 3 = battery present, bit 5 = VBUS good.
 *   - STATUS2 (0x01): bits[7:5] charge state, 0b001 == charging.
 *   - IC_TYPE (0x03): chip id, 0x4A for the AXP2101.
 *   - ADC_CHANNEL_CTRL (0x30): bit 0 = VBAT ADC, bit 1 = TS-pin ADC.
 *   - ADC_DATA_VBAT (0x34/0x35): 13-bit H5L8 battery voltage in mV.
 *   - BAT_PERCENT (0xA4): fuel-gauge percentage, read directly.
 */

#include "axp2101.h"

#include "driver/i2c.h"
#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"  /* pdMS_TO_TICKS — direct include */

static const char *TAG = "axp2101";

#define AXP_I2C_PORT      I2C_NUM_0
#define AXP_I2C_SDA       15     /* IIC_SDA — shared peripheral bus */
#define AXP_I2C_SCL       14     /* IIC_SCL */
#define AXP_I2C_HZ        400000 /* matches qmi8658.c / touch.c on the shared bus */
#define AXP_ADDR          0x34   /* AXP2101_SLAVE_ADDRESS */
#define AXP_I2C_TIMEOUT   pdMS_TO_TICKS(50)

#define REG_STATUS1       0x00
#define REG_STATUS2       0x01
#define REG_IC_TYPE       0x03
#define REG_ADC_CHAN_CTRL 0x30
#define REG_ADC_VBAT_H    0x34
#define REG_ADC_VBAT_L    0x35
#define REG_BAT_PERCENT   0xA4

#define AXP2101_CHIP_ID   0x4A

#define STATUS1_BAT_PRESENT_BIT 3
#define STATUS1_VBUS_GOOD_BIT   5
#define ADC_CHAN_VBAT_BIT       0
#define ADC_CHAN_TS_PIN_BIT     1

static bool s_initialised = false;

/* Install the I²C0 master on the 1.75"'s shared bus. Idempotent: shared
 * with touch.c (CST9217) and qmi8658.c (IMU). A redundant install is
 * ESP_ERR_INVALID_STATE on some IDF versions but ESP_FAIL on v6.0.1 —
 * either way the bus is up, so any install error is treated as "already
 * installed, proceed". The chip-id probe in axp2101_init is the real gate. */
static esp_err_t ensure_i2c_bus(void)
{
    const i2c_config_t cfg = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = AXP_I2C_SDA,
        .scl_io_num = AXP_I2C_SCL,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = AXP_I2C_HZ,
    };
    esp_err_t err = i2c_param_config(AXP_I2C_PORT, &cfg);
    if (err != ESP_OK) {
        return err;
    }
    err = i2c_driver_install(AXP_I2C_PORT, cfg.mode, 0, 0, 0);
    if (err != ESP_OK) {
        ESP_LOGD(TAG, "i2c_driver_install: %s — assuming shared bus already up",
                 esp_err_to_name(err));
    }
    return ESP_OK;  /* the chip-id probe is the real gate, not the install code */
}

static esp_err_t read_reg(uint8_t reg, uint8_t *out)
{
    return i2c_master_write_read_device(AXP_I2C_PORT, AXP_ADDR, &reg, 1,
                                        out, 1, AXP_I2C_TIMEOUT);
}

static esp_err_t write_reg(uint8_t reg, uint8_t value)
{
    const uint8_t buf[2] = { reg, value };
    return i2c_master_write_to_device(AXP_I2C_PORT, AXP_ADDR, buf,
                                      sizeof buf, AXP_I2C_TIMEOUT);
}

bool axp2101_init(void)
{
    esp_err_t bus = ensure_i2c_bus();
    if (bus != ESP_OK) {
        ESP_LOGW(TAG, "I2C bus install failed (%s) — battery telemetry disabled",
                 esp_err_to_name(bus));
        return false;
    }

    uint8_t id = 0;
    esp_err_t err = read_reg(REG_IC_TYPE, &id);
    if (err != ESP_OK || id != AXP2101_CHIP_ID) {
        ESP_LOGW(TAG, "no AXP2101 at 0x%02x (err=%s id=0x%02x) — battery telemetry disabled",
                 AXP_ADDR, esp_err_to_name(err), id);
        return false;
    }

    /* Read-modify-write the ADC channel control: enable the battery-voltage
     * ADC (so getBattVoltage has data) and disable the TS-pin measurement.
     * Disabling TS matters: with a thermistor-less pack the vendor demo
     * notes that leaving NTC detection on "will cause abnormal charging". */
    uint8_t adc = 0;
    if (read_reg(REG_ADC_CHAN_CTRL, &adc) == ESP_OK) {
        adc |= (uint8_t)(1u << ADC_CHAN_VBAT_BIT);
        adc &= (uint8_t)~(1u << ADC_CHAN_TS_PIN_BIT);
        esp_err_t w = write_reg(REG_ADC_CHAN_CTRL, adc);
        if (w != ESP_OK) {
            ESP_LOGW(TAG, "ADC channel config write failed (%s) — proceeding",
                     esp_err_to_name(w));
        }
    }

    s_initialised = true;
    ESP_LOGI(TAG, "ready @0x%02x (chip id 0x4A)", AXP_ADDR);
    return true;
}

bool axp2101_read(axp2101_status_t *out)
{
    if (!s_initialised || out == NULL) {
        return false;
    }

    uint8_t status1 = 0;
    uint8_t status2 = 0;
    if (read_reg(REG_STATUS1, &status1) != ESP_OK ||
        read_reg(REG_STATUS2, &status2) != ESP_OK) {
        return false;
    }

    const bool present  = (status1 >> STATUS1_BAT_PRESENT_BIT) & 0x01;
    const bool vbus     = (status1 >> STATUS1_VBUS_GOOD_BIT) & 0x01;
    /* STATUS2 bits[7:5]: 0b001 == charging (0b010 == discharging, 0b000
     * == standby). Mirrors XPowersLib isCharging(). */
    const bool charging = ((status2 >> 5) & 0x07) == 0x01;

    out->present  = present;
    out->vbus     = vbus;
    out->charging = charging;

    if (!present) {
        out->percent    = -1;
        out->millivolts = 0;
        return true;
    }

    uint8_t pct = 0;
    if (read_reg(REG_BAT_PERCENT, &pct) != ESP_OK) {
        return false;
    }
    if (pct > 100) {
        pct = 100;
    }
    out->percent = (int8_t)pct;

    uint8_t vh = 0;
    uint8_t vl = 0;
    if (read_reg(REG_ADC_VBAT_H, &vh) != ESP_OK ||
        read_reg(REG_ADC_VBAT_L, &vl) != ESP_OK) {
        return false;
    }
    /* 13-bit H5L8 assembly: high register carries the top 5 bits, low
     * register the bottom 8. The result is already in millivolts. */
    out->millivolts = (uint16_t)(((vh & 0x1F) << 8) | vl);
    return true;
}
