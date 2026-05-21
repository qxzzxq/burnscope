#pragma once

#include "lvgl.h"

/*
 * driver.h — internal helper exposed by the cyd2usb_st7789 profile's
 * driver.c to its own ui.c. Not part of the public profile interface;
 * file-private to this profile directory.
 */

/**
 * Initialise the ST7789 320x240 panel + LVGL port and return the display
 * handle. Idempotent — call once during `display_profile_init`.
 */
lv_display_t *cyd2usb_st7789_driver_init(void);
