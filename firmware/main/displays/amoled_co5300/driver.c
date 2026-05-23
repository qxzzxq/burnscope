/*
 * Waveshare ESP32-S3-Touch-AMOLED-1.43 bring-up.
 *
 * Uses the managed component `espressif/esp_lcd_sh8601` which supports
 * the QSPI command framing (cmd=0x02 + 24-bit-address phase carrying
 * the real command) that this AMOLED family expects. The Waveshare 1.43
 * board ships with either an SH8601 or a CO5300 driver IC — both speak
 * the same QSPI protocol, so this driver works for either.
 *
 * Pin assignments verified against the Waveshare 1.43 schematic
 * (GPIO/AMOLED column): see #define block below.
 *
 * Touch: CST820 (I2C on IO47/IO48) — NOT used in MVP.
 */

#include "driver.h"

#include "esp_err.h"
#include "esp_log.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_sh8601.h"
#include "esp_lvgl_port.h"

static const char *TAG = "amoled_drv";

#define LCD_HOST            SPI2_HOST
#define PIN_NUM_LCD_SCLK    10   /* OLED_CLK  */
#define PIN_NUM_LCD_D0      11   /* OLED_SIO0 */
#define PIN_NUM_LCD_D1      12   /* OLED_SI1  */
#define PIN_NUM_LCD_D2      13   /* OLED_SI2  */
#define PIN_NUM_LCD_D3      14   /* OLED_SI3  */
#define PIN_NUM_LCD_CS      9    /* OLED_CS   */
#define PIN_NUM_LCD_RST     21   /* OLED_RESET */

#define LCD_H_RES           466
#define LCD_V_RES           466
#define LCD_BIT_PER_PIXEL   16
/* 80 MHz is the speed Arduino_GFX uses on this exact board. */
#define LCD_PIXEL_CLOCK_HZ  (80 * 1000 * 1000)

/* SH8601 / CO5300 init register sequence for the Waveshare 1.43 panel.
 * Mirrors Moon-Arduino_GFX's `co5300_init_operations`. The SH8601 driver
 * applies these on top of its own default sequence, so we deliberately
 * skip 0x3A (COLMOD) — the driver sets it from `bits_per_pixel` and
 * warns "command has been used and will be overwritten" if we do too. */
static const sh8601_lcd_init_cmd_t s_amoled_init_cmds[] = {
    /* Page select / vendor command unlock. */
    { 0xFE, (uint8_t[]){ 0x00 }, 1, 0 },
    /* SPI mode control — keep panel in QSPI for pixel data. */
    { 0xC4, (uint8_t[]){ 0x80 }, 1, 0 },
    /* Display control 1. */
    { 0x53, (uint8_t[]){ 0x20 }, 1, 0 },
    /* HBM-mode brightness (max). */
    { 0x63, (uint8_t[]){ 0xFF }, 1, 0 },
    /* Sleep-out, then 120 ms settle. */
    { 0x11, NULL, 0, 120 },
    /* Display on, 20 ms settle. */
    { 0x29, NULL, 0, 20 },
    /* Normal-mode brightness. */
    { 0x51, (uint8_t[]){ 0xD0 }, 1, 0 },
    /* Contrast enhancement off. */
    { 0x58, (uint8_t[]){ 0x00 }, 1, 0 },
};

static lv_display_t *s_display = NULL;

lv_display_t *amoled_co5300_driver_init(void)
{
    if (s_display != NULL) {
        return s_display;
    }

    /* QSPI bus — single host carrying 4 data lines + SCLK + CS. data4-7
     * must be -1 (we're quad, not octal) or the SPI driver tries to claim
     * GPIO 0 and warns about a conflict. */
    const spi_bus_config_t buscfg = SH8601_PANEL_BUS_QSPI_CONFIG(
        PIN_NUM_LCD_SCLK,
        PIN_NUM_LCD_D0, PIN_NUM_LCD_D1, PIN_NUM_LCD_D2, PIN_NUM_LCD_D3,
        LCD_H_RES * 80 * sizeof(uint16_t));
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &buscfg, SPI_DMA_CH_AUTO));

    /* Panel IO over QSPI — the SH8601 component knows how to wrap each
     * command in the cmd=0x02 / addr=(cmd<<8) framing the panel expects. */
    esp_lcd_panel_io_handle_t io_handle = NULL;
    const esp_lcd_panel_io_spi_config_t io_config =
        SH8601_PANEL_IO_QSPI_CONFIG(PIN_NUM_LCD_CS, NULL, NULL);
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(
        (esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config, &io_handle));

    /* Panel — vendor_config carries the init register table + QSPI flag. */
    const sh8601_vendor_config_t vendor_config = {
        .init_cmds = s_amoled_init_cmds,
        .init_cmds_size = sizeof(s_amoled_init_cmds) / sizeof(s_amoled_init_cmds[0]),
        .flags = {
            .use_qspi_interface = 1,
        },
    };
    const esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = PIN_NUM_LCD_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = LCD_BIT_PER_PIXEL,
        .vendor_config = (void *)&vendor_config,
    };
    esp_lcd_panel_handle_t panel_handle = NULL;
    ESP_ERROR_CHECK(esp_lcd_new_panel_sh8601(io_handle, &panel_config, &panel_handle));

    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel_handle, true));

    /* LVGL port — 80-row stripe buffers, DMA-friendly, RGB565 with the
     * byte swap LVGL's RGB565 format needs for big-endian-on-wire SPI. */
    const lvgl_port_cfg_t lvgl_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    ESP_ERROR_CHECK(lvgl_port_init(&lvgl_cfg));

    const lvgl_port_display_cfg_t disp_cfg = {
        .io_handle = io_handle,
        .panel_handle = panel_handle,
        .buffer_size = LCD_H_RES * 80,
        .double_buffer = true,
        .hres = LCD_H_RES,
        .vres = LCD_V_RES,
        .monochrome = false,
        .rotation = {
            .swap_xy = false,
            .mirror_x = false,
            .mirror_y = false,
        },
        .color_format = LV_COLOR_FORMAT_RGB565,
        .flags = {
            .buff_dma = true,
            .swap_bytes = true,
        },
    };
    s_display = lvgl_port_add_disp(&disp_cfg);
    if (s_display == NULL) {
        ESP_LOGE(TAG, "lvgl_port_add_disp failed");
    }
    return s_display;
}
