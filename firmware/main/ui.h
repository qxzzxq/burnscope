#pragma once

#include "lvgl.h"

/*
 * ui.h — Phase 1 splash screen.
 *
 * Owns a single LVGL screen with three labels: a static "BurnScope" title,
 * a status line, and a version footer. `ui_set_status` is the only mutable
 * surface and is safe to call from any task (it grabs the lvgl_port lock).
 */

/**
 * Build the splash screen on the given display.
 *
 * Idempotent for the lifetime of the program — call exactly once after
 * `panel_init`. Initial status text is "Booting…".
 */
void ui_init(lv_display_t *disp);

/**
 * Replace the status line with the given UTF-8 text.
 *
 * Safe to call from any FreeRTOS task; serialises via the lvgl_port lock.
 * Passing NULL clears the line.
 */
void ui_set_status(const char *text);
