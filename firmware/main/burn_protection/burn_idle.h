#ifndef BURNSCOPE_BURN_IDLE_H
#define BURNSCOPE_BURN_IDLE_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * burn_idle — pure-logic idle state machine for OLED burn-in mitigation.
 *
 * Tracks "is the user present?" via a `last_activity_us` timestamp and
 * collapses idle duration into one of three states: ACTIVE → DIMMED → OFF.
 * The adapter (one per display profile) is responsible for translating
 * hardware events into the event enum below, driving a periodic EV_TIME
 * tick, and applying the returned `{brightness_pct, panel_on}` outputs to
 * the panel driver.
 *
 * Purity contract: this module compiles cleanly with a host C99 compiler.
 * No ESP-IDF, FreeRTOS, LVGL, or panel-driver includes. All time arguments
 * are monotonic microseconds — the SM has no opinion on the clock source.
 *
 * Threading: single-threaded. The adapter serialises calls into
 * burn_idle_step (event queue or mutex held across the call).
 *
 * See docs/fsd/oled-burn-in-mitigation-fsd.md § 6.2.1 for the spec.
 */

typedef enum {
    BURN_IDLE_ACTIVE = 0,
    BURN_IDLE_DIMMED,
    BURN_IDLE_OFF,
} burn_idle_state_t;

typedef enum {
    BURN_IDLE_EV_MOTION = 0,  /* accelerometer delta above threshold       */
    BURN_IDLE_EV_TOUCH,       /* capacitive touch event                    */
    BURN_IDLE_EV_BUTTON,      /* physical button press                     */
    BURN_IDLE_EV_PUSH,        /* POST /summary received (dedup'd upstream) */
    BURN_IDLE_EV_TIME,        /* re-evaluate against current time          */
} burn_idle_event_t;

typedef struct {
    int64_t dim_after_us;            /* idle duration before DIMMED       */
    int64_t off_after_us;            /* idle duration before OFF; >= dim  */
    uint8_t active_brightness_pct;   /* 0..100; e.g. 70                   */
    uint8_t dimmed_brightness_pct;   /* 0..100; e.g. 20; <= active        */
    int16_t motion_threshold_mg;     /* carried for the adapter; the SM
                                      * does not consume this field. Lives
                                      * here so adapters and the SM share
                                      * one configuration record.         */
} burn_idle_config_t;

typedef struct {
    burn_idle_state_t state;
    uint8_t           brightness_pct;
    bool              panel_on;
    bool              changed;       /* true iff output differs from prev */
} burn_idle_output_t;

typedef struct {
    burn_idle_state_t  state;
    int64_t            last_activity_us;
    burn_idle_config_t cfg;
    /* Internal — used only to compute output.changed. Not part of the
     * stable API; do not read or modify from outside this module.        */
    burn_idle_output_t _prev_output;
} burn_idle_t;

/**
 * Validate a config in isolation.
 *
 * Returns false when any invariant is violated:
 *   - dim_after_us <= 0
 *   - off_after_us <  dim_after_us  (equal is permitted; OFF wins at boundary)
 *   - active_brightness_pct  > 100
 *   - dimmed_brightness_pct  > 100
 *   - dimmed_brightness_pct  > active_brightness_pct
 *   - motion_threshold_mg    < 0
 *   - cfg == NULL
 *
 * Independently testable; callers may use it to vet a dynamic config
 * before passing it to burn_idle_init (which asserts internally).
 */
bool burn_idle_config_valid(const burn_idle_config_t *cfg);

/**
 * Initialise sm in BURN_IDLE_ACTIVE with last_activity_us = 0. Records
 * the post-init output as the baseline so the first subsequent step's
 * `changed` flag reports honestly.
 *
 * Asserts burn_idle_config_valid(&cfg).
 */
void burn_idle_init(burn_idle_t *sm, burn_idle_config_t cfg);

/**
 * Step the SM with event `ev` at monotonic time `now_us`.
 *
 * Direct-interaction wake events (MOTION/TOUCH/BUTTON) reset
 * last_activity_us to now_us and transition the SM to ACTIVE.
 *
 * PUSH is a soft wake — see FR-2.3. From OFF or DIMMED it lands in (or
 * stays in) DIMMED with last_activity_us = now_us - dim_after_us, so
 * the SM falls back to OFF after (off_after_us - dim_after_us) more
 * silence. From ACTIVE it refreshes last_activity_us without changing
 * state.
 *
 * EV_TIME re-evaluates `now - last_activity_us` against the configured
 * thresholds.
 *
 * Returns the new {state, brightness_pct, panel_on, changed} tuple.
 * `changed` is true iff any non-`changed` field differs from the
 * immediately-previous output.
 */
burn_idle_output_t burn_idle_step(burn_idle_t *sm,
                                  burn_idle_event_t ev,
                                  int64_t now_us);

#ifdef __cplusplus
}
#endif

#endif  /* BURNSCOPE_BURN_IDLE_H */
