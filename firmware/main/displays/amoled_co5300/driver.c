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

/* CO5300 init register sequence for the Waveshare 1.43 panel.
 *
 * Mirrored verbatim from Waveshare's own ESP-IDF demo
 * (`ESP-IDF/07_LVGL_Test/main/example_qspi_with_ram.c`, `co5300_lcd_init_cmds`).
 * Notable order vs. the Arduino_GFX version we started with:
 *   - SLPOUT first (wake the chip before vendor-register writes);
 *   - brightness ramp: 0x51=0x00 before DISPON, 0x51=0xFF after — keeps
 *     whatever junk is in the framebuffer from flashing at full
 *     brightness for one frame while LVGL is still booting.
 *
 * The commented entries are kept as breadcrumbs from the vendor demo:
 *   - 0x44/0x35 are TE (tearing-effect) setup, only needed if we wire
 *     the TE line to GPIO (we don't);
 *   - 0x36 is MADCTL (0x60 = the vendor's hardware-rotation hint). We
 *     do software rotation in LVGL instead, so leave MADCTL at default. */
static const sh8601_lcd_init_cmd_t s_amoled_init_cmds[] = {
    { 0x11, NULL, 0, 80 },                     /* SLPOUT, 80ms settle */
    { 0xC4, (uint8_t[]){ 0x80 }, 1, 0 },       /* SPIMODECTL: stay in QSPI */
    /* { 0x44, (uint8_t[]){ 0x01, 0xD1 }, 2, 0 }, // TE scanline target */
    /* { 0x35, (uint8_t[]){ 0x00 }, 1, 0 },       // TE ON */
    { 0x53, (uint8_t[]){ 0x20 }, 1, 1 },       /* WCTRLD1 */
    { 0x63, (uint8_t[]){ 0xFF }, 1, 1 },       /* HBM brightness max */
    { 0x51, (uint8_t[]){ 0x00 }, 1, 1 },       /* brightness 0 before DISPON */
    { 0x29, NULL, 0, 10 },                     /* DISPON */
    { 0x51, (uint8_t[]){ 0xFF }, 1, 0 },       /* brightness ramp to max */
    /* { 0x36, (uint8_t[]){ 0x60 }, 1, 0 },       // MADCTL hint (we SW-rotate) */
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

    /* 40-row stripe → 3 buffers × 466 × 40 × 2 B = ~109 KB in internal
     * RAM. Waveshare's demo uses MALLOC_CAP_DMA (internal RAM) with two
     * 116-row stripes; LVGL 9 + sw_rotate adds a third scratch buffer,
     * so we shrink the stripe to keep all three in DRAM. PSRAM-backed
     * buffers cause DMA TX underflows at the panel's QSPI clock. */
    const lvgl_port_display_cfg_t disp_cfg = {
        .io_handle = io_handle,
        .panel_handle = panel_handle,
        .buffer_size = LCD_H_RES * 40,
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
            /* SH8601 / CO5300 has no hardware swap_xy, so we rotate in
             * LVGL. Matches Waveshare's own demo
             * (`disp_drv.sw_rotate = 1; disp_drv.rotated = LV_DISP_ROT_270`). */
            .sw_rotate = true,
        },
    };
    s_display = lvgl_port_add_disp(&disp_cfg);
    if (s_display == NULL) {
        ESP_LOGE(TAG, "lvgl_port_add_disp failed");
        return NULL;
    }

    /* 270° rotation puts logical (0,0) at the panel's physical bottom-
     * right when the USB-C connector is at the bottom of the board.
     * Equivalent to LV_DISP_ROT_270 in the Waveshare LVGL-8 demo. */
    if (lvgl_port_lock(0)) {
        lv_display_set_rotation(s_display, LV_DISPLAY_ROTATION_270);
        lvgl_port_unlock();
    }
    return s_display;
}
