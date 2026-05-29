/*
 * Orientation watcher — IMU-driven auto-rotate for the Waveshare 1.75"
 * AMOLED. Clone of the 1.43" profile's orientation.c; the only board
 * difference is the brightness call (amoled_co5300_175_*) and the boot
 * rotation baseline (ROTATION_0 here vs 270 on the 1.43").
 *
 * Reads the QMI8658 accelerometer at ~10 Hz, classifies which of ±X /
 * ±Y carries the dominant gravity component, applies hysteresis and
 * debounce, and calls `lv_display_set_rotation` on the registered
 * display when a stable new orientation appears.
 *
 * Z-axis is intentionally ignored (lying flat is not an "orientation"
 * the panel can render — it just keeps the last one).
 *
 * Threading: a single FreeRTOS task owns all reads of the QMI8658
 * (concurrent with the burn_idle adapter's IMU sampler — the I²C
 * driver serialises bus access internally) and all writes to LVGL's
 * rotation state (under lvgl_port_lock).
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
#define HYSTERESIS_MG       200      /* 0.2 g band */
#define DEBOUNCE_US         500000   /* 500 ms */
/* 8 KB: lv_refr_now in apply_rotation runs the full LVGL render
 * pipeline on this task's stack (draw → sw_rotate → flush callback →
 * panel-IO). The lvgl_port task uses 7 KB by default for the same
 * work; we match it with a small safety margin. */
#define TASK_STACK          8192
#define TASK_PRIORITY       2        /* below burn_idle's IMU sampler */
/* Rotation-dip fade timings. The LVGL stripe-by-stripe redraw kicked
 * off by lv_display_set_rotation is visibly ugly on a 466×466 panel
 * with 40-row stripes. We fade the panel dark before the rotation,
 * drain the redraw synchronously via lv_refr_now while the panel is at
 * 0 %, then fade back up. The whole transition runs *under*
 * lvgl_port_lock and writes the 0x51 register directly from this task —
 * not via the burn-in adapter's esp_timer fade engine. FADE_STEP_MS
 * sets the inline ramp's cadence; 16 ms ≈ 60 Hz. */
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
 * to "the panel should render with this rotation".
 *
 * Calibrated on the Waveshare 1.75" hardware (axis directions per the
 * vendor docs, https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.75).
 * The mapping is a clean +90° progression around the clockwise pose
 * cycle (down → left → up → right), which is the consistency check that
 * any correct table must satisfy:
 *
 *   USB-C down  → gravity in -Y → AXIS_Y_NEG → ROTATION_90
 *   USB-C left  → gravity in -X → AXIS_X_NEG → ROTATION_180
 *   USB-C up    → gravity in +Y → AXIS_Y_POS → ROTATION_270
 *   USB-C right → gravity in +X → AXIS_X_POS → ROTATION_0
 *
 * Note this board's QMI8658 has a sizeable +X accelerometer offset
 * (~+600 mg), which makes AXIS_X_POS the "sticky" default the detector
 * falls into during handling. That means a move *to* USB-C-right often
 * doesn't re-animate (the display is already in X_POS) — but X_POS maps
 * to ROTATION_0 here, which is USB-C-right's correct upright value, so
 * the end state is right regardless. The log line
 * "rotate: accel=(x,y,z) mg → axis=N rot=M" identifies the chosen axis
 * per pose if a future board rev moves the IMU.
 */
static const lv_display_rotation_t s_axis_to_rotation[] = {
    [AXIS_X_POS] = LV_DISPLAY_ROTATION_0,     /* USB-C right */
    [AXIS_X_NEG] = LV_DISPLAY_ROTATION_180,   /* USB-C left  */
    [AXIS_Y_POS] = LV_DISPLAY_ROTATION_270,   /* USB-C up    */
    [AXIS_Y_NEG] = LV_DISPLAY_ROTATION_90,    /* USB-C down  */
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
 * be called with lvgl_port_lock already held. */
static void ramp_brightness_locked(uint8_t from_pct, uint8_t to_pct, uint32_t duration_ms)
{
    const int steps = (int)(duration_ms / FADE_STEP_MS);
    if (steps <= 0) {
        amoled_co5300_175_set_brightness_pct(to_pct);
        return;
    }
    const int delta = (int)to_pct - (int)from_pct;
    for (int i = 1; i <= steps; ++i) {
        const int pct = (int)from_pct + (delta * i) / steps;
        amoled_co5300_175_set_brightness_pct((uint8_t)pct);
        vTaskDelay(pdMS_TO_TICKS(FADE_STEP_MS));
    }
}

/* Apply a new rotation, masked by a brightness dip so the LVGL
 * stripe-by-stripe redraw isn't visible. The lock is held continuously
 * through fade-down, rotation, redraw, and fade-up — this is the key to
 * a clean fade (releasing it between steps lets the 1 Hz tick / snapshot
 * pushes interleave and stutter the ramp). The adapter is anchored at
 * the current brightness so its esp_timer fade engine stays dormant for
 * the duration. */
static void apply_rotation(lv_display_rotation_t rot)
{
    const uint8_t restore_pct = burn_idle_adapter_current_brightness_pct();
    if (restore_pct == 0) {
        /* Panel is dark already (e.g. idle-OFF). Just flip the rotation
         * flag and exit; the next wake-up's fade-in renders in the new
         * orientation. */
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

    /* Track the rotation we last *applied*. driver.c boots the panel at
     * ROTATION_0, which maps to AXIS_X_POS (USB-C right) in the table
     * above; seed the state machine there so a board already in that
     * pose doesn't trigger a spurious first rotation. Everything is
     * re-derived from the first stable accelerometer sample regardless. */
    axis_t current_axis        = AXIS_X_POS;         /* matches driver boot ROTATION_0 */
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

        /* Hysteresis: a new axis must beat the *current* axis's reading
         * by at least HYSTERESIS_MG. This rejects the 45° boundary where
         * both axes show ~707 mg. */
        if (proposed != current_axis) {
            const int16_t cur_reading = axis_reading(current_axis, accel[0], accel[1]);
            const int16_t new_reading = axis_reading(proposed,     accel[0], accel[1]);
            if (new_reading - cur_reading < HYSTERESIS_MG) {
                proposed = current_axis;
            }
        }

        const int64_t now = esp_timer_get_time();

        /* Debounce: the proposed axis must hold steady for DEBOUNCE_US
         * before we commit. A fresh proposal resets the timer; the same
         * proposal accumulates time. */
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
    /* Diagnostic builds: rotation is frozen at the driver's boot
     * setting, while burn_idle's IMU sampler is unaffected and still
     * emits EV_MOTION for the wake path. */
}

#endif
