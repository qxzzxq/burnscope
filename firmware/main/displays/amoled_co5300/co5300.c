/*
 * co5300.c — Chipsemi CO5300 round-AMOLED panel driver.
 *
 * Implements the esp_lcd_panel_t interface so esp_lvgl_port can drive
 * the 466×466 round AMOLED on the Waveshare ESP32-S3-Touch-AMOLED-1.43
 * board. Commands are sent on a single SPI data line; pixel data is
 * pushed in quad-SPI mode (configured at the panel-IO layer above us).
 *
 * Init sequence is the publicly documented CO5300 power-on default
 * (sleep-out → COLMOD RGB565 → MADCTL default orientation → tearing-
 * effect enable → brightness max → display-on). Address-window writes
 * use a 6-column offset (typical for CO5300 round panels where the
 * visible disc is centred inside a larger framebuffer); verify on
 * hardware and tweak `CO5300_COL_OFFSET` / `CO5300_ROW_OFFSET` if the
 * pixels appear shifted or wrapped.
 *
 * UNTESTED on hardware. The structure follows ESP-IDF's vendor panel
 * pattern (cf. components/esp_lcd/src/esp_lcd_panel_st7789.c); on-board
 * verification is needed to confirm the init sequence and offsets.
 */

#include "co5300.h"

#include <stdlib.h>
#include <string.h>

#include "esp_check.h"
#include "esp_log.h"
#include "esp_lcd_panel_commands.h"
#include "esp_lcd_panel_dev.h"
#include "esp_lcd_panel_interface.h"
#include "esp_lcd_panel_io.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"

static const char *TAG = "co5300";

/* Address-window offsets: the CO5300 framebuffer is wider than the
 * visible 466×466 disc, so column/row writes start at a non-zero
 * offset. The classic default for CO5300 round panels is 6 columns.
 * Adjust if the on-board image is shifted left/right. */
#define CO5300_COL_OFFSET 6
#define CO5300_ROW_OFFSET 0

typedef struct {
    esp_lcd_panel_t base;
    esp_lcd_panel_io_handle_t io;
    int reset_gpio;
    bool reset_active_high;
    uint8_t madctl;   /* current MADCTL value (driven by mirror/swap_xy) */
    uint8_t colmod;   /* current COLMOD (RGB565 / RGB666 / RGB888) */
    int x_gap;
    int y_gap;
} co5300_panel_t;

static esp_err_t co5300_send_cmd(co5300_panel_t *p, uint8_t cmd,
                                 const void *data, size_t len)
{
    return esp_lcd_panel_io_tx_param(p->io, cmd, data, len);
}

static esp_err_t co5300_reset(esp_lcd_panel_t *panel)
{
    co5300_panel_t *p = __containerof(panel, co5300_panel_t, base);
    if (p->reset_gpio >= 0) {
        gpio_set_level(p->reset_gpio, p->reset_active_high ? 0 : 1);
        vTaskDelay(pdMS_TO_TICKS(10));
        gpio_set_level(p->reset_gpio, p->reset_active_high ? 1 : 0);
        vTaskDelay(pdMS_TO_TICKS(120));
    } else {
        ESP_RETURN_ON_ERROR(co5300_send_cmd(p, LCD_CMD_SWRESET, NULL, 0),
                            TAG, "sw-reset failed");
        vTaskDelay(pdMS_TO_TICKS(120));
    }
    return ESP_OK;
}

static esp_err_t co5300_init(esp_lcd_panel_t *panel)
{
    co5300_panel_t *p = __containerof(panel, co5300_panel_t, base);

    /* SLPOUT — wake from sleep, wait the datasheet-mandated 120 ms. */
    ESP_RETURN_ON_ERROR(co5300_send_cmd(p, LCD_CMD_SLPOUT, NULL, 0),
                        TAG, "SLPOUT");
    vTaskDelay(pdMS_TO_TICKS(120));

    /* COLMOD — pixel format. 0x55 = 16-bit RGB565 (matches our LVGL
     * buffer); 0x66 = 18-bit RGB666; 0x77 = 24-bit RGB888. */
    ESP_RETURN_ON_ERROR(co5300_send_cmd(p, LCD_CMD_COLMOD, &p->colmod, 1),
                        TAG, "COLMOD");

    /* MADCTL — memory access (mirror/swap/BGR). Default = 0 (RGB,
     * no mirror, no swap). Mutated by mirror()/swap_xy() ops. */
    ESP_RETURN_ON_ERROR(co5300_send_cmd(p, LCD_CMD_MADCTL, &p->madctl, 1),
                        TAG, "MADCTL");

    /* WRDISBV — set brightness to max (0xFF) by default. The host can
     * later call co5300_set_brightness() to dim the AMOLED. */
    uint8_t brightness = 0xFF;
    ESP_RETURN_ON_ERROR(co5300_send_cmd(p, 0x51, &brightness, 1),
                        TAG, "WRDISBV");

    /* WRCTRLD — enable backlight / dimming control (BCTRL=1, DD=1, BL=1). */
    uint8_t ctrld = 0x2C;
    ESP_RETURN_ON_ERROR(co5300_send_cmd(p, 0x53, &ctrld, 1),
                        TAG, "WRCTRLD");

    /* DISPON. */
    ESP_RETURN_ON_ERROR(co5300_send_cmd(p, LCD_CMD_DISPON, NULL, 0),
                        TAG, "DISPON");
    vTaskDelay(pdMS_TO_TICKS(20));

    return ESP_OK;
}

static esp_err_t co5300_del(esp_lcd_panel_t *panel)
{
    co5300_panel_t *p = __containerof(panel, co5300_panel_t, base);
    free(p);
    return ESP_OK;
}

static esp_err_t co5300_draw_bitmap(esp_lcd_panel_t *panel,
                                    int x_start, int y_start,
                                    int x_end, int y_end,
                                    const void *color_data)
{
    co5300_panel_t *p = __containerof(panel, co5300_panel_t, base);

    /* CASET — column window. Pixels [x_start, x_end). */
    const int xs = x_start + p->x_gap + CO5300_COL_OFFSET;
    const int xe = x_end   + p->x_gap + CO5300_COL_OFFSET - 1;
    uint8_t cas[4] = { (xs >> 8) & 0xFF, xs & 0xFF, (xe >> 8) & 0xFF, xe & 0xFF };
    ESP_RETURN_ON_ERROR(co5300_send_cmd(p, LCD_CMD_CASET, cas, 4),
                        TAG, "CASET");

    /* RASET — row window. Pixels [y_start, y_end). */
    const int ys = y_start + p->y_gap + CO5300_ROW_OFFSET;
    const int ye = y_end   + p->y_gap + CO5300_ROW_OFFSET - 1;
    uint8_t ras[4] = { (ys >> 8) & 0xFF, ys & 0xFF, (ye >> 8) & 0xFF, ye & 0xFF };
    ESP_RETURN_ON_ERROR(co5300_send_cmd(p, LCD_CMD_RASET, ras, 4),
                        TAG, "RASET");

    /* RAMWR — push pixels. Color data length in bytes. */
    const size_t bpp = (p->colmod == 0x55) ? 2 : (p->colmod == 0x66 ? 3 : 3);
    const size_t pixels = (size_t)(x_end - x_start) * (size_t)(y_end - y_start);
    return esp_lcd_panel_io_tx_color(p->io, LCD_CMD_RAMWR, color_data, pixels * bpp);
}

static esp_err_t co5300_invert_color(esp_lcd_panel_t *panel, bool invert)
{
    co5300_panel_t *p = __containerof(panel, co5300_panel_t, base);
    return co5300_send_cmd(p, invert ? LCD_CMD_INVON : LCD_CMD_INVOFF, NULL, 0);
}

static esp_err_t co5300_mirror(esp_lcd_panel_t *panel, bool mirror_x, bool mirror_y)
{
    co5300_panel_t *p = __containerof(panel, co5300_panel_t, base);
    if (mirror_x) p->madctl |= 0x40; else p->madctl &= ~0x40;
    if (mirror_y) p->madctl |= 0x80; else p->madctl &= ~0x80;
    return co5300_send_cmd(p, LCD_CMD_MADCTL, &p->madctl, 1);
}

static esp_err_t co5300_swap_xy(esp_lcd_panel_t *panel, bool swap)
{
    co5300_panel_t *p = __containerof(panel, co5300_panel_t, base);
    if (swap) p->madctl |= 0x20; else p->madctl &= ~0x20;
    return co5300_send_cmd(p, LCD_CMD_MADCTL, &p->madctl, 1);
}

static esp_err_t co5300_set_gap(esp_lcd_panel_t *panel, int x_gap, int y_gap)
{
    co5300_panel_t *p = __containerof(panel, co5300_panel_t, base);
    p->x_gap = x_gap;
    p->y_gap = y_gap;
    return ESP_OK;
}

static esp_err_t co5300_disp_on_off(esp_lcd_panel_t *panel, bool on)
{
    co5300_panel_t *p = __containerof(panel, co5300_panel_t, base);
    return co5300_send_cmd(p, on ? LCD_CMD_DISPON : LCD_CMD_DISPOFF, NULL, 0);
}

static esp_err_t co5300_disp_sleep(esp_lcd_panel_t *panel, bool sleep)
{
    co5300_panel_t *p = __containerof(panel, co5300_panel_t, base);
    return co5300_send_cmd(p, sleep ? LCD_CMD_SLPIN : LCD_CMD_SLPOUT, NULL, 0);
}

esp_err_t esp_lcd_new_panel_co5300(esp_lcd_panel_io_handle_t io,
                                   const esp_lcd_panel_dev_config_t *cfg,
                                   esp_lcd_panel_handle_t *out)
{
    ESP_RETURN_ON_FALSE(io && cfg && out, ESP_ERR_INVALID_ARG, TAG, "null arg");

    co5300_panel_t *p = calloc(1, sizeof(*p));
    ESP_RETURN_ON_FALSE(p, ESP_ERR_NO_MEM, TAG, "no mem");

    p->io = io;
    p->reset_gpio = cfg->reset_gpio_num;
    p->reset_active_high = cfg->flags.reset_active_high;
    p->madctl = 0;
    /* Default to RGB565 — matches the LVGL buffer format we register. */
    p->colmod = (cfg->bits_per_pixel == 16) ? 0x55
              : (cfg->bits_per_pixel == 18) ? 0x66 : 0x77;

    if (p->reset_gpio >= 0) {
        gpio_config_t gpio_cfg = {
            .mode = GPIO_MODE_OUTPUT,
            .pin_bit_mask = 1ULL << p->reset_gpio,
        };
        esp_err_t err = gpio_config(&gpio_cfg);
        if (err != ESP_OK) {
            free(p);
            return err;
        }
    }

    p->base.reset         = co5300_reset;
    p->base.init          = co5300_init;
    p->base.del           = co5300_del;
    p->base.draw_bitmap   = co5300_draw_bitmap;
    p->base.invert_color  = co5300_invert_color;
    p->base.mirror        = co5300_mirror;
    p->base.swap_xy       = co5300_swap_xy;
    p->base.set_gap       = co5300_set_gap;
    p->base.disp_on_off   = co5300_disp_on_off;
    p->base.disp_sleep    = co5300_disp_sleep;

    *out = &p->base;
    return ESP_OK;
}

esp_err_t co5300_set_brightness(esp_lcd_panel_handle_t panel, uint8_t value)
{
    co5300_panel_t *p = __containerof(panel, co5300_panel_t, base);
    return co5300_send_cmd(p, 0x51, &value, 1);
}
