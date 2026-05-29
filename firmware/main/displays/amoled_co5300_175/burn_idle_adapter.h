#pragma once

/*
 * burn_idle_adapter — AMOLED-side wiring between the pure `burn_idle`
 * state machine (`burn_protection/burn_idle.h`) and the Waveshare 1.75"
 * (CO5300) board's hardware.
 *
 * Clone of the amoled_sh8601 (1.43") adapter, trimmed for this board:
 * no IMU (so no EV_MOTION sampler) and no touch (so no EV_TOUCH). That
 * leaves three event sources:
 *   - 1 Hz esp_timer                          → EV_TIME
 *   - snapshot store listener (POST /summary) → EV_PUSH
 *   - GPIO0 (BOOT button) negative-edge ISR   → EV_BUTTON
 *
 * Outputs applied:
 *   - `amoled_co5300_175_set_brightness_pct` on every state change
 *   - `amoled_co5300_175_set_display_on(true/false)` on panel_on transitions
 *
 * Threading: a single drain task pops events and calls into the SM.
 * Producers (ISR, timer, listener) only post to the FreeRTOS queue, so
 * the SM's single-threaded contract is preserved.
 */

/**
 * Initialise the SM with config sourced from Kconfig, install the
 * snapshot listener, start the 1 Hz tick timer, attach the GPIO0 ISR,
 * and spawn the drain task. Idempotent.
 */
void burn_idle_adapter_start(void);
