#pragma once

/*
 * burn_idle_adapter — AMOLED-side wiring between the pure
 * `burn_idle` state machine (`burn_protection/burn_idle.h`) and the
 * board's hardware.
 *
 * Event sources fed into the SM:
 *   - 1 Hz esp_timer                              → EV_TIME
 *   - snapshot store listener (POST /summary)     → EV_PUSH
 *   - GPIO0 (BOOT button) negative-edge ISR       → EV_BUTTON
 *   - LVGL touch indev callback (notify_touch)    → EV_TOUCH
 *
 * Outputs applied:
 *   - `amoled_sh8601_set_brightness_pct` on every state change
 *   - `amoled_sh8601_set_display_on(true/false)` on panel_on transitions
 *
 * IMU motion (EV_MOTION) ships in PR-2 alongside the QMI8658 port.
 *
 * Threading: a single drain task pops events and calls into the SM.
 * Producers (ISR, timer, listener, touch callback) only post to the
 * FreeRTOS queue, so the SM's single-threaded contract is preserved.
 */

/**
 * Initialise the SM with config sourced from Kconfig, install the
 * snapshot listener, start the 1 Hz tick timer, attach the GPIO0 ISR,
 * and spawn the drain task. Idempotent.
 */
void burn_idle_adapter_start(void);

/**
 * Post EV_TOUCH into the adapter's event queue. Called by the LVGL
 * touch input-device callback on a fresh-press (released → pressed)
 * transition. Non-blocking; drops the event if the queue is full
 * (next tick will re-evaluate the SM anyway).
 */
void burn_idle_adapter_notify_touch(void);
