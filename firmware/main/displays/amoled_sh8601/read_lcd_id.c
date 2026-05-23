/*
 * Bit-banged single-line SPI read of RDID1 (0xDA) to detect SH8601 vs
 * CO5300 silicon. See read_lcd_id.h for the contract.
 *
 * The CS/SCLK/D0..D3/RST pins start as GPIOs at boot. This routine
 * drives them by hand to send a QSPI-framed read command + read one
 * byte back on D0, then leaves them as is — `spi_bus_initialize()`
 * re-routes them through the GPIO matrix when it claims the bus.
 *
 * The CS handling here matches Waveshare's reference verbatim: CS is
 * not toggled around the read transaction, only the clock and D0 lines
 * are pulsed.
 */

#include "read_lcd_id.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "amoled_id";

#define PIN_CS    9
#define PIN_SCLK  10
#define PIN_D0    11
#define PIN_D1    12
#define PIN_D2    13
#define PIN_D3    14
#define PIN_RST   21

#define PIN_BIT(p) (1ULL << (p))

static void gpio_init_outputs(void)
{
    gpio_config_t cfg = {
        .intr_type = GPIO_INTR_DISABLE,
        .mode = GPIO_MODE_OUTPUT,
        .pin_bit_mask = PIN_BIT(PIN_CS) | PIN_BIT(PIN_SCLK) |
                        PIN_BIT(PIN_D0) | PIN_BIT(PIN_D1) |
                        PIN_BIT(PIN_D2) | PIN_BIT(PIN_D3) |
                        PIN_BIT(PIN_RST),
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&cfg);
}

static void d0_input(void)
{
    gpio_config_t cfg = {
        .intr_type = GPIO_INTR_DISABLE,
        .mode = GPIO_MODE_INPUT,
        .pin_bit_mask = PIN_BIT(PIN_D0),
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&cfg);
}

static void d0_output(void)
{
    gpio_config_t cfg = {
        .intr_type = GPIO_INTR_DISABLE,
        .mode = GPIO_MODE_OUTPUT,
        .pin_bit_mask = PIN_BIT(PIN_D0),
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&cfg);
}

static void shift_out_byte(uint8_t b)
{
    for (int i = 0; i < 8; i++) {
        gpio_set_level(PIN_D0, (b & 0x80) ? 1 : 0);
        b <<= 1;
        gpio_set_level(PIN_SCLK, 0);
        gpio_set_level(PIN_SCLK, 1);
    }
}

static uint8_t shift_in_byte(void)
{
    uint8_t dat = 0;
    for (int i = 0; i < 8; i++) {
        gpio_set_level(PIN_SCLK, 0);
        d0_input();
        esp_rom_delay_us(1);
        dat = (dat << 1) | (uint8_t)gpio_get_level(PIN_D0);
        d0_output();
        gpio_set_level(PIN_SCLK, 1);
        esp_rom_delay_us(1);
    }
    return dat;
}

uint8_t amoled_sh8601_read_lcd_id(void)
{
    gpio_init_outputs();

    /* Power-cycle the panel via RST so the read sees a clean state. */
    gpio_set_level(PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(120));
    gpio_set_level(PIN_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(120));
    gpio_set_level(PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(120));

    /* QSPI read framing: cmd 0x03, 24-bit address with the real reg in
     * the middle byte, then read one data byte. */
    shift_out_byte(0x03);
    shift_out_byte(0x00);
    shift_out_byte(0xDA);
    shift_out_byte(0x00);
    uint8_t id = shift_in_byte();

    ESP_LOGI(TAG, "LCD RDID1 = 0x%02x", id);
    return id;
}
