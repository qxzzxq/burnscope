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

#include "qmi8658.h"

#ifdef CONFIG_BURNSCOPE_AMOLED_ORIENTATION_AUTO

static const char *TAG = "orientation";

#define SAMPLE_PERIOD_MS    100      /* ~10 Hz */
#define HYSTERESIS_MG       200      /* FR-1.3: 0.2 g band */
#define DEBOUNCE_US         500000   /* FR-1.4: 500 ms */
#define TASK_STACK          3072
#define TASK_PRIORITY       2        /* below burn_idle's IMU sampler */
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

static void apply_rotation(lv_display_rotation_t rot)
{
    if (!lvgl_port_lock(0)) {
        ESP_LOGW(TAG, "lvgl_port_lock failed; skipping rotation update");
        return;
    }
    lv_display_set_rotation(s_display, rot);
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
