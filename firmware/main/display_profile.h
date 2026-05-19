#pragma once

#include "snapshot.h"

/*
 * display_profile.h — the build-time-selected panel + UI bundle.
 *
 * Each display profile lives under `displays/<name>/` and owns *both* its
 * panel bring-up and its LVGL layout, because UI design is geometry-bound.
 * The build picks exactly one profile via Kconfig (`CONFIG_BURNSCOPE_DISPLAY_*`)
 * and links its driver + ui translation units; main.c calls only the symbols
 * declared below and never sees `lv_display_t *` or panel internals.
 *
 * To add a new screen, drop in `displays/<name>/{driver.c,ui.c}`, expose a
 * `bool` Kconfig under the `BURNSCOPE_DISPLAY` choice, and gate the new
 * sources from `main/CMakeLists.txt`.
 */

/**
 * Bring up the panel + LVGL, install the splash screen, and arm the 1 Hz
 * tick timer. Idempotent — safe to call once at boot.
 */
void display_profile_init(void);

/**
 * Show a single-line status message on the splash screen. Safe from any
 * task (the profile internally takes the LVGL port lock). Passing NULL
 * clears the line.
 */
void display_profile_show_status(const char *text);

/**
 * Notify the display that a fresh snapshot has landed in the store. Only
 * the *first* such call transitions off the splash onto the agent screen;
 * subsequent calls leave the visible screen untouched so a Codex push
 * doesn't yank rotation away from a currently-visible Claude row (and
 * vice versa). The 1 Hz LVGL timer installed during init handles
 * re-render against the (already-updated) snapshot store and cycling
 * between agents (FR-4.10), so callers don't need to drive ticks.
 *
 * If `snap` is NULL the splash returns. Safe from any task.
 */
void display_profile_show_agent(const agent_snapshot_t *snap);
