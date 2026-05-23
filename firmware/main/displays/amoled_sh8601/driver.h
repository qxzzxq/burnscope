#pragma once

#include "lvgl.h"

/*
 * driver.h — internal helper exposed by the amoled_sh8601 profile's
 * driver.c to its own ui.c. File-private to this profile directory;
 * not part of the public profile interface.
 */

/**
 * Bring up the 466×466 Waveshare 1.43" round AMOLED (SH8601 or CO5300
 * silicon — same QSPI protocol) and register the LVGL display. Returns
 * the LVGL display handle, or NULL on failure. Idempotent — call once
 * during `display_profile_init`.
 */
lv_display_t *amoled_sh8601_driver_init(void);
