/*
 * Orientation watcher — Phase 3 of the OLED burn-in mitigation FSD.
 *
 * Reads the QMI8658 accelerometer at ~10 Hz (the chip itself runs at
 * Qmi8658AccOdr_LowPower_21Hz per FR-1.1; we under-sample because
 * humans don't rotate devices faster than ~1 Hz and the 500 ms
 * debounce gives us 5 samples of headroom). Classifies which of ±X /
 * ±Y carries the dominant gravity component, applies hysteresis and
 * debounce, and calls `lv_display_set_rotation` on the registered
 * display when a stable new orientation appears.
 *
 * Axis-to-rotation mapping is empirical and lives in
 * `s_axis_to_rotation` below — if a board revision moves the IMU or
 * the panel ribbon, that table is the only thing to edit.
 *
 * Z-axis is intentionally ignored (lying flat is not an "orientation"
 * the panel can render — it just keeps the last one).
 *
 * Threading: a single FreeRTOS task owns all reads of the QMI8658
 * (concurrent with the burn_idle adapter's IMU sampler — the I²C
 * driver serialises bus access internally) and all writes to LVGL's
 * rotation state (under lvgl_port_lock, same contract as the driver
 * and ui.c).
 */

#include "orientation.h"

#include <stdint.h>

#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"

#include "burn_idle_adapter.h"
#include "driver.h"
#include "qmi8658.h"

#ifdef CONFIG_BURNSCOPE_AMOLED_ORIENTATION_AUTO

static const char *TAG = "orientation";

#define SAMPLE_PERIOD_MS    100      /* ~10 Hz */
#define HYSTERESIS_MG       200      /* FR-1.3: 0.2 g band */
#define DEBOUNCE_US         500000   /* FR-1.4: 500 ms */
/* 8 KB: lv_refr_now in apply_rotation runs the full LVGL render
 * pipeline on this task's stack (draw → sw_rotate → flush callback →
 * panel-IO). The lvgl_port task uses 7 KB by default for the same
 * work; we match it with a small safety margin. The earlier 3 KB
 * sufficed only when this task did nothing but lv_display_set_rotation. */
#define TASK_STACK          8192
#define TASK_PRIORITY       2        /* below burn_idle's IMU sampler */
/* Rotation-dip fade timings. The LVGL stripe-by-stripe redraw kicked
 * off by lv_display_set_rotation is visibly ugly on a 466×466 panel
 * with 40-row stripes (~12 visible bands). We fade the panel dark
 * before the rotation, drain the redraw synchronously via lv_refr_now
 * while the panel is at 0 %, then fade back up.
 *
 * The whole transition runs *under* lvgl_port_lock and writes the 0x51
 * register directly from this task — not via the burn-in adapter's
 * esp_timer fade engine. Holding the lock for the entire transition
 * prevents any other LVGL work (1 Hz countdown tick, snapshot pushes,
 * touch indev reads) from racing the fade, which was the root cause
 * of the "brightness freezes then catches up" stutter — those
 * contending lv_timer_handler calls used to hold the lock for tens of
 * ms at a time, blocking adapter-driven fade callbacks queued behind
 * them. FADE_STEP_MS sets the inline ramp's cadence; 16 ms ≈ 60 Hz so
 * the human eye sees a continuous gradient. */
#define FADE_DOWN_MS        300
#define FADE_UP_MS          300
#define FADE_STEP_MS        16
/* Below the dominant axis's mg reading we treat the sample as
 * inconclusive (device lying flat, free fall, vigorous shake). The
 * QMI8658 reports ~1000 mg under steady gravity; 400 mg comfortably
 * covers the cos(60°) = 500 mg edge while rejecting samples with no
 * clear preferred axis. */
#define DOMINANT_MIN_MG     400

typedef enum {
    AXIS_X_POS = 0,
    AXIS_X_NEG,
    AXIS_Y_POS,
    AXIS_Y_NEG,
} axis_t;

/* Empirical mapping from "the gravity vector points in this direction"
 * to "the panel should render with this rotation". Each entry is the
 * rotation that puts the UI's footer at whichever physical edge of
 * the device gravity is currently pulling toward (i.e. upright from
 * the user's perspective).
 *
 * Calibrated on the bench against the Waveshare 1.43" board. The
 * IMU's +X axis points along the panel from the USB-C edge toward
 * the opposite (BAT) edge, and +Y is the orthogonal in-plane axis.
 * That makes the four canonical poses:
 *   USB-C bottom → gravity in -X → AXIS_X_NEG → ROTATION_90
 *   USB-C top    → gravity in +X → AXIS_X_POS → ROTATION_270
 *   USB-C right  → gravity in +Y → AXIS_Y_POS → ROTATION_180
 *   USB-C left   → gravity in -Y → AXIS_Y_NEG → ROTATION_0
 * If a future board rev moves the IMU or flips the panel ribbon,
 * permute this table — log line
 * "rotate: accel=(x,y,z) mg → axis=N rot=M" identifies which axis
 * is being chosen for each physical pose.
 */
static const lv_display_rotation_t s_axis_to_rotation[] = {
    [AXIS_X_POS] = LV_DISPLAY_ROTATION_270,
    [AXIS_X_NEG] = LV_DISPLAY_ROTATION_90,   /* USB-C at bottom — calibrated */
    [AXIS_Y_POS] = LV_DISPLAY_ROTATION_180,
    [AXIS_Y_NEG] = LV_DISPLAY_ROTATION_0,
};

static lv_display_t *s_display = NULL;
static bool          s_started = false;

static int16_t abs_i16(int16_t v) { return v < 0 ? (int16_t)-v : v; }

/* Pick the axis whose absolute mg reading is largest. Returns false if
 * neither ±X nor ±Y exceeds DOMINANT_MIN_MG — the caller should treat
 * that as "no opinion, keep current orientation." */
static bool dominant_axis(int16_t ax, int16_t ay, axis_t *out)
{
    const int16_t abs_x = abs_i16(ax);
    const int16_t abs_y = abs_i16(ay);
    int16_t dominant_mag;
    if (abs_x > abs_y) {
        *out = ax > 0 ? AXIS_X_POS : AXIS_X_NEG;
        dominant_mag = abs_x;
    } else {
        *out = ay > 0 ? AXIS_Y_POS : AXIS_Y_NEG;
        dominant_mag = abs_y;
    }
    return dominant_mag >= DOMINANT_MIN_MG;
}

/* Signed gravity component along an axis, in mg. */
static int16_t axis_reading(axis_t axis, int16_t ax, int16_t ay)
{
    switch (axis) {
    case AXIS_X_POS: return  ax;
    case AXIS_X_NEG: return (int16_t)-ax;
    case AXIS_Y_POS: return  ay;
    case AXIS_Y_NEG: return (int16_t)-ay;
    }
    return 0;
}

/* Run an inline brightness ramp from `from_pct` to `to_pct` over
 * `duration_ms`, writing the 0x51 register directly each step. Must
 * be called with lvgl_port_lock already held — `set_brightness_pct`
 * re-takes the lock recursively, which is cheap, but the surrounding
 * caller relies on holding the lock to keep other LVGL work out of
 * the way (see apply_rotation's contract).
 */
static void ramp_brightness_locked(uint8_t from_pct, uint8_t to_pct, uint32_t duration_ms)
{
    const int steps = (int)(duration_ms / FADE_STEP_MS);
    if (steps <= 0) {
        amoled_sh8601_set_brightness_pct(to_pct);
        return;
    }
    const int delta = (int)to_pct - (int)from_pct;
    for (int i = 1; i <= steps; ++i) {
        const int pct = (int)from_pct + (delta * i) / steps;
        amoled_sh8601_set_brightness_pct((uint8_t)pct);
        vTaskDelay(pdMS_TO_TICKS(FADE_STEP_MS));
    }
}

/* Apply a new rotation, masked by a brightness dip so the LVGL
 * stripe-by-stripe redraw isn't visible. Steps:
 *   1. Snapshot the burn-in adapter's current brightness — the level
 *      we'll restore to. May be ACTIVE (70 %) or DIMMED (20 %)
 *      depending on idle state. If the panel is already off (0 %),
 *      there's nothing to mask, so we apply the rotation cheaply and
 *      return.
 *   2. Take lvgl_port_lock for the whole transition. The lock is
 *      held continuously through fade-down, rotation, redraw, and
 *      fade-up — this is the key to a clean fade. The original
 *      esp_timer-driven implementation released the lock between
 *      each brightness step, letting LVGL's 1 Hz countdown tick
 *      and snapshot pushes interleave with the ramp; those renders
 *      held the lock for tens of ms each, queueing fade callbacks
 *      behind them and producing the "freeze then catch up" stutter.
 *   3. Anchor the burn-in adapter at the current brightness so its
 *      fade engine stays dormant for the duration. Without this,
 *      a TIME-tick state change mid-transition could fire a
 *      competing fade whose esp_timer callbacks would queue behind
 *      our lock and snap the panel to a different target the moment
 *      we release.
 *   4. Inline ramp from `restore_pct` → 0 % over FADE_DOWN_MS.
 *   5. lv_display_set_rotation + lv_refr_now drains the full-screen
 *      redraw synchronously while the panel is at 0 %.
 *   6. Inline ramp from 0 % → `restore_pct` over FADE_UP_MS.
 *   7. Re-anchor the adapter at restore_pct so the very next adapter
 *      fade blends from the correct value.
 *
 * Cost: the LVGL lock is held for FADE_DOWN_MS + redraw + FADE_UP_MS
 * (~750 ms at current timings). During that window other LVGL work
 * (snapshot updates, touch indev reads, 1 Hz countdown) is deferred.
 * Rotations are infrequent and the deferred work catches up
 * immediately on release, so this is acceptable.
 */
static void apply_rotation(lv_display_rotation_t rot)
{
    const uint8_t restore_pct = burn_idle_adapter_current_brightness_pct();
    if (restore_pct == 0) {
        /* Panel is dark already (e.g. idle-OFF). Just flip the rotation
         * flag and exit; the next wake-up's fade-in will render in the
         * new orientation. */
        if (lvgl_port_lock(0)) {
            lv_display_set_rotation(s_display, rot);
            lvgl_port_unlock();
        }
        return;
    }

    if (!lvgl_port_lock(0)) {
        ESP_LOGW(TAG, "lvgl_port_lock failed; skipping rotation update");
        return;
    }

    burn_idle_adapter_anchor_brightness_pct(restore_pct);

    ramp_brightness_locked(restore_pct, 0, FADE_DOWN_MS);

    lv_display_set_rotation(s_display, rot);
    lv_refr_now(s_display);

    ramp_brightness_locked(0, restore_pct, FADE_UP_MS);

    burn_idle_adapter_anchor_brightness_pct(restore_pct);

    lvgl_port_unlock();
}

static void orientation_task(void *arg)
{
    (void)arg;

    /* Track the rotation we last *applied*, not whatever LVGL might
     * have at boot — driver.c sets ROTATION_270, but we re-derive
     * everything below from accelerometer readings so the first
     * stable sample brings the panel into agreement. */
    axis_t current_axis        = AXIS_X_POS;        /* matches driver boot ROT_270 */
    axis_t candidate_axis      = AXIS_X_POS;
    int64_t candidate_since_us = INT64_MAX;          /* "no pending candidate" */

    bool first_failure_logged = false;
    const TickType_t period   = pdMS_TO_TICKS(SAMPLE_PERIOD_MS);
    TickType_t last_wake      = xTaskGetTickCount();

    for (;;) {
        xTaskDelayUntil(&last_wake, period);

        int16_t accel[3];
        if (!qmi8658_read_accel_mg(accel)) {
            if (!first_failure_logged) {
                ESP_LOGW(TAG, "qmi8658_read_accel_mg failed; orientation idle");
                first_failure_logged = true;
            }
            continue;
        }
        first_failure_logged = false;

        axis_t proposed;
        if (!dominant_axis(accel[0], accel[1], &proposed)) {
            /* No clear dominant axis (e.g. lying flat). Keep whatever
             * candidate state we had, but don't reset its timer — a
             * brief flat moment shouldn't reset a debounce that was
             * about to apply. */
            continue;
        }

        /* Hysteresis (FR-1.3): a new axis must beat the *current*
         * axis's reading by at least HYSTERESIS_MG. This rejects the
         * 45° boundary where both axes show ~707 mg. */
        if (proposed != current_axis) {
            const int16_t cur_reading = axis_reading(current_axis, accel[0], accel[1]);
            const int16_t new_reading = axis_reading(proposed,     accel[0], accel[1]);
            if (new_reading - cur_reading < HYSTERESIS_MG) {
                proposed = current_axis;
            }
        }

        const int64_t now = esp_timer_get_time();

        /* Debounce (FR-1.4): the proposed axis must hold steady for
         * DEBOUNCE_US before we commit. A fresh proposal resets the
         * timer; the same proposal accumulates time. */
        if (proposed != candidate_axis) {
            candidate_axis     = proposed;
            candidate_since_us = now;
            continue;
        }
        if (candidate_axis == current_axis) {
            /* Nothing pending. */
            continue;
        }
        if ((now - candidate_since_us) < DEBOUNCE_US) {
            continue;
        }

        const lv_display_rotation_t rot = s_axis_to_rotation[candidate_axis];
        ESP_LOGI(TAG, "rotate: accel=(%d,%d,%d) mg → axis=%d rot=%d",
                 accel[0], accel[1], accel[2],
                 (int)candidate_axis, (int)rot);
        apply_rotation(rot);
        current_axis = candidate_axis;
    }
}

void orientation_start(lv_display_t *disp)
{
    if (s_started || disp == NULL) {
        return;
    }
    s_display = disp;

    BaseType_t ok = xTaskCreate(orientation_task, "orientation",
                                TASK_STACK, NULL, TASK_PRIORITY, NULL);
    if (ok != pdPASS) {
        ESP_LOGW(TAG, "orientation task spawn failed; rotation disabled");
        return;
    }
    s_started = true;
    ESP_LOGI(TAG,
             "started: hysteresis=%d mg, debounce=%d ms, sample=%d ms",
             HYSTERESIS_MG, (int)(DEBOUNCE_US / 1000), SAMPLE_PERIOD_MS);
}

#else  /* CONFIG_BURNSCOPE_AMOLED_ORIENTATION_AUTO not set */

void orientation_start(lv_display_t *disp)
{
    (void)disp;
    /* Diagnostic builds (FR-1.6, IMU-ROT-004): rotation is frozen at
     * the driver's boot setting, while burn_idle's IMU sampler is
     * unaffected and still emits EV_MOTION for the wake path. */
}

#endif
