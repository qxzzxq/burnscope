#pragma once

#include "esp_err.h"
#include "esp_lcd_panel_dev.h"
#include "esp_lcd_panel_io.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Minimal CO5300 vendor panel driver, modelled on the ESP-IDF vendor
 * panel API (mirrors `esp_lcd_new_panel_st7789`). The CO5300 is a
 * 466×466 round AMOLED controller spoken to over single-data-line
 * commands + quad-SPI pixel data; this driver hides the address-window
 * + pixel push behind the standard `esp_lcd_panel_*` ops so the rest
 * of the firmware can stay panel-agnostic via esp_lvgl_port.
 *
 * The init sequence is the publicly documented CO5300 power-on default
 * with the column/row offsets the panel needs (the framebuffer is
 * larger than the visible disc — see init code).
 *
 * UNTESTED on hardware: written without board access. Verify pixel
 * order, byte order, and the address-window offsets when the device
 * arrives. See `firmware/main/displays/amoled_co5300/README` markers
 * in this file.
 */
esp_err_t esp_lcd_new_panel_co5300(esp_lcd_panel_io_handle_t io,
                                   const esp_lcd_panel_dev_config_t *cfg,
                                   esp_lcd_panel_handle_t *out);

/** Set AMOLED brightness via the CO5300 0x51 register (0x00 – 0xFF). */
esp_err_t co5300_set_brightness(esp_lcd_panel_handle_t panel, uint8_t value);

#ifdef __cplusplus
}
#endif
