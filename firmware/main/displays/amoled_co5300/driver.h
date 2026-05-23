#pragma once

#include "lvgl.h"

/*
 * driver.h — internal helper exposed by the amoled_co5300 profile's
 * driver.c to its own ui.c. File-private to this profile directory;
 * not part of the public profile interface.
 */

/**
 * Bring up the 466×466 CO5300 round AMOLED over QSPI and register the
 * LVGL display. Returns the LVGL display handle, or NULL on failure.
 * Idempotent — call once during `display_profile_init`.
 */
lv_display_t *amoled_co5300_driver_init(void);
