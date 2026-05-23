/*
 * Waveshare ESP32-S3-Touch-AMOLED-1.43 bring-up.
 *
 * Pinout per https://www.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.43
 * (the schematic PDF linked from that page). UNVERIFIED on hardware —
 * these constants are the documented defaults for this board family but
 * must be confirmed against the actual device's schematic before flash.
 *
 * Display: CO5300, 466×466, QSPI (commands single-line, pixels quad).
 * Touch:   CST820 over I2C — NOT used in MVP (render-only profile).
 * Memory:  ESP32-S3 with 8 MB PSRAM; ~8 MB flash. PSRAM unused for the
 *          framebuffer (an 8 KB scanline-bundle is the LVGL working buf,
 *          allocated from DRAM via esp_lvgl_port).
 */

#include "driver.h"
#include "co5300.h"

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lvgl_port.h"

static const char *TAG = "amoled_drv";

/* QSPI bus — single SPI host carrying the 4 data lines + clock + CS.
 * The CO5300 receives commands on D0 only (1-bit mode); pixel data is
 * transferred in 4-bit mode automatically via the panel-IO config flag.
 */
#define LCD_HOST            SPI2_HOST
#define PIN_NUM_LCD_SCLK    47
#define PIN_NUM_LCD_D0      46
#define PIN_NUM_LCD_D1      45
#define PIN_NUM_LCD_D2      42
#define PIN_NUM_LCD_D3      41
#define PIN_NUM_LCD_CS      9
#define PIN_NUM_LCD_RST     17
#define PIN_NUM_LCD_TE      18   /* tearing-effect; informational only */

/* Panel geometry — round AMOLED, square framebuffer with hardware mask. */
#define LCD_H_RES           466
#define LCD_V_RES           466
/* 80 MHz is comfortable for quad-SPI on the S3's GPIO matrix; raise to
 * 120 MHz only after confirming bit errors on long pushes are absent. */
#define LCD_PIXEL_CLOCK_HZ  (80 * 1000 * 1000)

static lv_display_t *s_display = NULL;

static esp_err_t init_qspi_bus(void)
{
    /* data0_io_num through data3_io_num cover the quad data lines.
     * In ESP-IDF, data0_io_num aliases mosi_io_num and data1_io_num
     * aliases miso_io_num — set them via the data* names only to
     * avoid the duplicate-initialiser warning. */
    const spi_bus_config_t buscfg = {
        .sclk_io_num = PIN_NUM_LCD_SCLK,
        .data0_io_num = PIN_NUM_LCD_D0,
        .data1_io_num = PIN_NUM_LCD_D1,
        .data2_io_num = PIN_NUM_LCD_D2,
        .data3_io_num = PIN_NUM_LCD_D3,
        /* Big enough to hold an 80-row stripe at 16 bpp. */
        .max_transfer_sz = LCD_H_RES * 80 * sizeof(uint16_t),
        .flags = SPICOMMON_BUSFLAG_QUAD,
    };
    return spi_bus_initialize(LCD_HOST, &buscfg, SPI_DMA_CH_AUTO);
}

static esp_lcd_panel_handle_t init_co5300_panel(esp_lcd_panel_io_handle_t *io_out)
{
    ESP_ERROR_CHECK(init_qspi_bus());

    /* Panel-IO over SPI. CO5300 expects 8-bit commands + 24-bit param
     * dummy phase (`lcd_param_bits = 8`). The `quad_mode` flag tells
     * the SPI driver to put data lines into 4-bit mode for the pixel
     * push (`tx_color`) while keeping commands in 1-bit mode. */
    esp_lcd_panel_io_handle_t io_handle = NULL;
    const esp_lcd_panel_io_spi_config_t io_config = {
        .cs_gpio_num = PIN_NUM_LCD_CS,
        .dc_gpio_num = -1,            /* command/data carried in-band on CO5300 */
        .spi_mode = 0,
        .pclk_hz = LCD_PIXEL_CLOCK_HZ,
        .trans_queue_depth = 10,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .flags = {
            .quad_mode = true,
        },
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(
        (esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config, &io_handle));

    /* Bring the panel up via our minimal CO5300 driver. */
    esp_lcd_panel_handle_t panel_handle = NULL;
    const esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = PIN_NUM_LCD_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
        .flags = { .reset_active_high = 0 },
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_co5300(io_handle, &panel_config, &panel_handle));

    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel_handle, true));

    *io_out = io_handle;
    return panel_handle;
}

lv_display_t *amoled_co5300_driver_init(void)
{
    if (s_display != NULL) {
        return s_display;
    }

    esp_lcd_panel_io_handle_t io_handle = NULL;
    esp_lcd_panel_handle_t panel_handle = init_co5300_panel(&io_handle);

    const lvgl_port_cfg_t lvgl_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    ESP_ERROR_CHECK(lvgl_port_init(&lvgl_cfg));

    /* 80-row stripe buffer; double-buffered (DMA-friendly). The S3 has
     * 8 MB PSRAM so we could buffer the full screen if we wanted, but a
     * stripe keeps DRAM pressure low and flush latency predictable. */
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
