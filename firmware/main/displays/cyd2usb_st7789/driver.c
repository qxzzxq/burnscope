/*
 * Cheap Yellow Display (cyd2usb variant) bring-up.
 *
 * Pinout per https://github.com/witnessmenow/ESP32-Cheap-Yellow-Display/blob/main/PINS.md.
 * The display SPI is *not* routed on IOMUX pins, so the pixel clock is
 * pinned at 20 MHz (empirically — 40 MHz produces bit errors). Panel is
 * BGR with inversion off (cyd2usb profile).
 */

#include "driver.h"

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_err.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lvgl_port.h"

/* CYD wiring — display SPI bus (separate from touch). */
#define LCD_HOST            SPI2_HOST
#define PIN_NUM_SCLK        14
#define PIN_NUM_MOSI        13
#define PIN_NUM_MISO        12
#define PIN_NUM_LCD_DC      2
#define PIN_NUM_LCD_RST     -1   /* Tied to system EN on CYD. */
#define PIN_NUM_LCD_CS      15
#define PIN_NUM_BK_LIGHT    21

/* ST7789 native orientation is 240x320 portrait; we rotate to 320x240. */
#define LCD_H_RES_NATIVE    240
#define LCD_H_RES           320
#define LCD_V_RES           240
/* CYD display SPI is not on IOMUX pins; 40 MHz causes bit errors (C-2). */
#define LCD_PIXEL_CLOCK_HZ  (20 * 1000 * 1000)

static lv_display_t *s_display = NULL;

/* Backlight is PWM-dimmed via LEDC. GPIO21 drives an active-high transistor,
 * so duty maps directly to brightness. Hardcoded to 50% for now. */
#define BK_LIGHT_LEDC_MODE      LEDC_LOW_SPEED_MODE
#define BK_LIGHT_LEDC_TIMER     LEDC_TIMER_0
#define BK_LIGHT_LEDC_CHANNEL   LEDC_CHANNEL_0
#define BK_LIGHT_LEDC_DUTY_RES  LEDC_TIMER_8_BIT   /* duty range 0–255 */
#define BK_LIGHT_LEDC_FREQ_HZ   5000               /* >1 kHz: no visible flicker */
#define BK_LIGHT_DUTY_PCT       50

static void enable_backlight(void)
{
    const ledc_timer_config_t timer = {
        .speed_mode      = BK_LIGHT_LEDC_MODE,
        .timer_num       = BK_LIGHT_LEDC_TIMER,
        .duty_resolution = BK_LIGHT_LEDC_DUTY_RES,
        .freq_hz         = BK_LIGHT_LEDC_FREQ_HZ,
        .clk_cfg         = LEDC_AUTO_CLK,
    };
    ESP_ERROR_CHECK(ledc_timer_config(&timer));

    const ledc_channel_config_t channel = {
        .gpio_num   = PIN_NUM_BK_LIGHT,
        .speed_mode = BK_LIGHT_LEDC_MODE,
        .channel    = BK_LIGHT_LEDC_CHANNEL,
        .timer_sel  = BK_LIGHT_LEDC_TIMER,
        .duty       = (BK_LIGHT_DUTY_PCT * 255) / 100,  /* 255 = full at 8-bit res */
        .hpoint     = 0,
    };
    ESP_ERROR_CHECK(ledc_channel_config(&channel));
}

static esp_lcd_panel_handle_t init_st7789(esp_lcd_panel_io_handle_t *io_out)
{
    const spi_bus_config_t buscfg = {
        .sclk_io_num = PIN_NUM_SCLK,
        .mosi_io_num = PIN_NUM_MOSI,
        .miso_io_num = PIN_NUM_MISO,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = LCD_H_RES_NATIVE * 40 * sizeof(uint16_t),
    };
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &buscfg, SPI_DMA_CH_AUTO));

    esp_lcd_panel_io_handle_t io_handle = NULL;
    const esp_lcd_panel_io_spi_config_t io_config = {
        .cs_gpio_num = PIN_NUM_LCD_CS,
        .dc_gpio_num = PIN_NUM_LCD_DC,
        .spi_mode = 0,
        .pclk_hz = LCD_PIXEL_CLOCK_HZ,
        .trans_queue_depth = 10,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(
        (esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config, &io_handle));

    esp_lcd_panel_handle_t panel_handle = NULL;
    const esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = PIN_NUM_LCD_RST,
        /* CYD ST7789 panels are physically RGB; setting BGR here would flip
         * the R and B channels (orange→blue), as seen with the brand icons. */
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(io_handle, &panel_config, &panel_handle));

    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_invert_color(panel_handle, false));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel_handle, true));

    *io_out = io_handle;
    return panel_handle;
}

lv_display_t *cyd2usb_st7789_driver_init(void)
{
    if (s_display != NULL) {
        return s_display;
    }

    enable_backlight();

    esp_lcd_panel_io_handle_t io_handle = NULL;
    esp_lcd_panel_handle_t panel_handle = init_st7789(&io_handle);

    const lvgl_port_cfg_t lvgl_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    ESP_ERROR_CHECK(lvgl_port_init(&lvgl_cfg));

    const lvgl_port_display_cfg_t disp_cfg = {
        .io_handle = io_handle,
        .panel_handle = panel_handle,
        .buffer_size = LCD_H_RES * 40,
        .double_buffer = true,
        .hres = LCD_H_RES,
        .vres = LCD_V_RES,
        .monochrome = false,
        .rotation = {
            .swap_xy = true,
            .mirror_x = true,
            .mirror_y = false,
        },
        .color_format = LV_COLOR_FORMAT_RGB565,
        .flags = {
            .buff_dma = true,
            .swap_bytes = true,
        },
    };
    s_display = lvgl_port_add_disp(&disp_cfg);
    return s_display;
}
