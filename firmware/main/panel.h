#pragma once

#include "lvgl.h"

/*
 * panel.h — CYD cyd2usb (ST7789 320x240 landscape) bring-up.
 *
 * panel_init() configures the SPI bus, ST7789 panel, backlight GPIO, and
 * registers the panel with esp_lvgl_port. The caller owns the returned
 * display handle and is expected to draw into it via the lvgl_port lock.
 */

/**
 * Initialise the CYD panel and register it with LVGL.
 *
 * Side effects: claims SPI2_HOST, GPIO21 (backlight), and the ST7789
 * panel pins listed in `panel.c`. Calls `lvgl_port_init` internally.
 *
 * Returns the LVGL display handle (never NULL — panics on failure).
 */
lv_display_t *panel_init(void);
