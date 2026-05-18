/*
 * BurnScope firmware — hello-world bring-up for the Cheap Yellow Display.
 *
 * Targets the cyd2usb variant (two USB ports): the panel is an ST7789, wired
 * BGR with inversion off. Pinout per
 * https://github.com/witnessmenow/ESP32-Cheap-Yellow-Display/blob/main/PINS.md.
 * LVGL (via esp_lvgl_port) renders "Hello, world!" centred on the panel.
 */

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_err.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "lvgl.h"

static const char *TAG = "burnscope";

/* CYD wiring — display SPI bus (separate from touch). */
#define LCD_HOST            SPI2_HOST
#define PIN_NUM_SCLK        14
#define PIN_NUM_MOSI        13
#define PIN_NUM_MISO        12
#define PIN_NUM_LCD_DC      2
#define PIN_NUM_LCD_RST     -1   /* Tied to system EN on CYD. */
#define PIN_NUM_LCD_CS      15
#define PIN_NUM_BK_LIGHT    21
#define LCD_BK_LIGHT_ON     1

/* ILI9341 native orientation is 240x320 portrait; we rotate to 320x240. */
#define LCD_H_RES_NATIVE    240
#define LCD_V_RES_NATIVE    320
#define LCD_H_RES           320
#define LCD_V_RES           240
/* CYD's display SPI is not on IOMUX pins; 40 MHz produces bit errors. */
#define LCD_PIXEL_CLOCK_HZ  (20 * 1000 * 1000)

static void enable_backlight(void)
{
    const gpio_config_t cfg = {
        .mode = GPIO_MODE_OUTPUT,
        .pin_bit_mask = 1ULL << PIN_NUM_BK_LIGHT,
    };
    ESP_ERROR_CHECK(gpio_config(&cfg));
    ESP_ERROR_CHECK(gpio_set_level(PIN_NUM_BK_LIGHT, LCD_BK_LIGHT_ON));
}

static esp_lcd_panel_handle_t init_panel(esp_lcd_panel_io_handle_t *io_out)
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
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR,
        .bits_per_pixel = 16,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(io_handle, &panel_config, &panel_handle));

    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel_handle));
    /* TFT_eSPI cyd2usb profile uses TFT_INVERSION_OFF. */
    ESP_ERROR_CHECK(esp_lcd_panel_invert_color(panel_handle, false));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel_handle, true));

    *io_out = io_handle;
    return panel_handle;
}

static void render_hello(lv_display_t *disp)
{
    if (!lvgl_port_lock(0)) {
        ESP_LOGE(TAG, "lvgl_port_lock failed");
        return;
    }
    lv_obj_t *scr = lv_display_get_screen_active(disp);
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);

    lv_obj_t *label = lv_label_create(scr);
    lv_label_set_text(label, "Hello, world!");
    lv_obj_set_style_text_color(label, lv_color_white(), 0);
    lv_obj_set_style_text_font(label, &lv_font_montserrat_24, 0);
    lv_obj_center(label);

    lvgl_port_unlock();
}

void app_main(void)
{
    enable_backlight();

    esp_lcd_panel_io_handle_t io_handle = NULL;
    esp_lcd_panel_handle_t panel_handle = init_panel(&io_handle);

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
    lv_display_t *disp = lvgl_port_add_disp(&disp_cfg);

    render_hello(disp);
    ESP_LOGI(TAG, "Hello, world rendered");
}
