#pragma once

/*
 * burn_idle_adapter — AMOLED-side wiring between the pure `burn_idle`
 * state machine (`burn_protection/burn_idle.h`) and the Waveshare 1.75"
 * (CO5300) board's hardware. Clone of the 1.43" adapter.
 *
 * Event sources fed into the SM:
 *   - 1 Hz esp_timer                              → EV_TIME
 *   - snapshot store listener (POST /summary)     → EV_PUSH
 *   - GPIO0 (BOOT button) negative-edge ISR       → EV_BUTTON
 *   - QMI8658 motion sampler                      → EV_MOTION
 *   - LVGL touch indev callback (notify_touch)    → EV_TOUCH (CST9217,
 *     polled from ui.c's touch_indev_read_cb on a fresh press)
 *
 * Outputs applied:
 *   - `amoled_co5300_175_set_brightness_pct` on every state change
 *   - `amoled_co5300_175_set_display_on(true/false)` on panel_on transitions
 *
 * Threading: a single drain task pops events and calls into the SM.
 * Producers (ISR, timer, listener, IMU sampler, touch callback) only
 * post to the FreeRTOS queue, so the SM's single-threaded contract is
 * preserved.
 */

/**
 * Initialise the SM with config sourced from Kconfig, install the
 * snapshot listener, start the 1 Hz tick timer, attach the GPIO0 ISR,
 * bring up the QMI8658 motion sampler, and spawn the drain task.
 * Idempotent.
 */
void burn_idle_adapter_start(void);

/**
 * Post EV_TOUCH into the adapter's event queue. Called by the LVGL touch
 * input-device callback (ui.c) on a fresh-press transition. Non-blocking;
 * drops the event if the queue is full.
 */
void burn_idle_adapter_notify_touch(void);

#include <stdint.h>

/**
 * Read the panel brightness the adapter's fade engine currently has
 * the panel at, in percent. Used by orientation.c to remember the
 * level to fade back up to after a rotation-dip blackout.
 */
uint8_t burn_idle_adapter_current_brightness_pct(void);

/**
 * Force the fade engine into "idle at this brightness, no pending
 * writes" state without touching the panel. Cancels any in-flight
 * adapter-initiated fade (bumps the generation counter so its
 * remaining fade_step_cb invocations drop their writes) and stops
 * the periodic fade timer.
 *
 * Used by orientation.c to take exclusive control of the brightness
 * register during a rotation transition.
 */
void burn_idle_adapter_anchor_brightness_pct(uint8_t pct);
