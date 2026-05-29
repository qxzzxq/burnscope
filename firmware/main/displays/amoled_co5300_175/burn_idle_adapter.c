/*
 * AMOLED idle adapter (Waveshare 1.75" / CO5300) — see
 * burn_idle_adapter.h for the role and event topology. Clone of the
 * amoled_sh8601 (1.43") adapter with the IMU motion sampler and touch
 * event source removed (this board has neither). Implementation notes:
 *
 *  - Brightness/sleep changes apply only when the SM reports
 *    `output.changed`; ordering is wake-first (DISPON before the
 *    fade-up begins) and sleep-last (DISPOFF only after the fade-down
 *    reaches 0) so a transitioning frame never flashes black before
 *    the panel sleeps or full-bright before a wake.
 *  - Brightness changes ramp via a linear fade. The CO5300 has no
 *    hardware fade primitive, so a dedicated esp_timer fires at ~30 Hz
 *    only while a fade is in flight, writing intermediate 0x51 register
 *    values; it stops itself on completion. A new fade preempts an
 *    in-flight one by re-anchoring `start_pct` to the *current*
 *    interpolated value, so a button press during a dim-down smoothly
 *    reverses direction rather than snapping.
 *  - The 1 Hz tick is driven by an `esp_timer_*` periodic callback that
 *    posts EV_TIME into the queue (never calls burn_idle_step directly,
 *    preserving the SM's single-threaded contract).
 *  - Button ISR debounce is in-ISR via `esp_timer_get_time()`.
 */

#include "burn_idle_adapter.h"

#include "burn_protection/burn_idle.h"
#include "driver.h"
#include "snapshot.h"

#include "driver/gpio.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static const char *TAG = "burn_idle_ad";

#define EVENT_QUEUE_DEPTH       8
#define TICK_PERIOD_US          1000000      /* 1 Hz */
#define BUTTON_GPIO             GPIO_NUM_0
#define BUTTON_DEBOUNCE_US      100000       /* 100 ms */
#define DRAIN_TASK_STACK        4096
#define DRAIN_TASK_PRIORITY     5
#define FADE_STEP_PERIOD_US     33000        /* ~30 Hz brightness ramp */

static burn_idle_t        s_sm;
static QueueHandle_t      s_queue       = NULL;
static esp_timer_handle_t s_tick_timer  = NULL;
static volatile int64_t   s_last_button_us = 0;
static bool               s_prev_panel_on = true;  /* matches SM init baseline */
static bool               s_started        = false;

/* Fade engine state. `current_pct` is the last value we wrote to the
 * 0x51 register and is the only source-of-truth for the panel's actual
 * brightness — start_fade() anchors a new ramp here so preempting
 * events never produce a visible step. `pending_panel_off` carries the
 * sleep-last contract: the DISPOFF command runs only after the fade
 * reaches 0, and is cleared whenever a subsequent fade redirects the
 * panel back toward ON (e.g. button mid-sleep). `generation` bumps on
 * every start_fade so an in-flight fade_step_cb that already snapshot
 * the previous fade's parameters can drop its hardware writes after a
 * preemption. All fields are read and written from two contexts (drain
 * task and esp_timer task) so accesses are guarded by `s_fade_mux`. */
static struct {
    uint8_t  start_pct;
    uint8_t  target_pct;
    uint8_t  current_pct;
    int64_t  start_us;
    int64_t  duration_us;
    uint32_t generation;
    bool     pending_panel_off;
    bool     active;
} s_fade;
static portMUX_TYPE       s_fade_mux    = portMUX_INITIALIZER_UNLOCKED;
static esp_timer_handle_t s_fade_timer  = NULL;

static void post_event(burn_idle_event_t ev)
{
    if (s_queue == NULL) {
        return;
    }
    (void)xQueueSend(s_queue, &ev, 0);  /* drop if full — next tick re-evaluates */
}

static void post_event_from_isr(burn_idle_event_t ev,
                                BaseType_t *higher_woken)
{
    if (s_queue == NULL) {
        return;
    }
    (void)xQueueSendFromISR(s_queue, &ev, higher_woken);
}

static void tick_timer_cb(void *arg)
{
    (void)arg;
    post_event(BURN_IDLE_EV_TIME);
}

static void IRAM_ATTR button_isr(void *arg)
{
    (void)arg;
    const int64_t now = esp_timer_get_time();
    if (now - s_last_button_us < BUTTON_DEBOUNCE_US) {
        return;
    }
    s_last_button_us = now;
    BaseType_t higher_woken = pdFALSE;
    post_event_from_isr(BURN_IDLE_EV_BUTTON, &higher_woken);
    if (higher_woken == pdTRUE) {
        portYIELD_FROM_ISR();
    }
}

static void on_snapshot_push(const agent_snapshot_t *snap, void *user)
{
    (void)snap;
    (void)user;
    post_event(BURN_IDLE_EV_PUSH);
}

static void fade_step_cb(void *arg)
{
    (void)arg;

    uint8_t  start_pct;
    uint8_t  target_pct;
    int64_t  start_us;
    int64_t  duration_us;
    uint32_t my_gen;

    /* Pass 1: snapshot inputs only. Mutations are deferred to Pass 2
     * (under a re-check'd lock) so a stale tick can't corrupt
     * s_fade.current_pct — start_fade() reads that field as the
     * anchor for the next ramp and would otherwise blend from a
     * value never actually written to the panel. */
    portENTER_CRITICAL(&s_fade_mux);
    if (!s_fade.active) {
        /* Late callback after a stop — nothing to do. */
        portEXIT_CRITICAL(&s_fade_mux);
        return;
    }
    my_gen      = s_fade.generation;
    start_pct   = s_fade.start_pct;
    target_pct  = s_fade.target_pct;
    start_us    = s_fade.start_us;
    duration_us = s_fade.duration_us;
    portEXIT_CRITICAL(&s_fade_mux);

    /* Compute next value — pure math against the snapshot. */
    const int64_t now     = esp_timer_get_time();
    const int64_t elapsed = now - start_us;
    uint8_t to_write;
    bool    finished;
    if (elapsed >= duration_us) {
        to_write = target_pct;
        finished = true;
    } else {
        const int delta  = (int)target_pct - (int)start_pct;
        const int interp = (int)start_pct
                         + (int)((int64_t)delta * elapsed / duration_us);
        to_write = (uint8_t)(interp < 0 ? 0 : (interp > 100 ? 100 : interp));
        finished = false;
    }

    /* Pass 2: re-check generation, then atomically commit state and
     * stop the timer on completion. Stopping under the lock — paired
     * with `s_fade.active = false` — makes it safe for a start_fade()
     * racing in immediately after this critical section to see
     * active=false and cleanly restart the timer for its own fade. */
    bool do_dispoff = false;
    portENTER_CRITICAL(&s_fade_mux);
    if (s_fade.generation != my_gen) {
        portEXIT_CRITICAL(&s_fade_mux);
        return;
    }
    s_fade.current_pct = to_write;
    if (finished) {
        s_fade.active            = false;
        do_dispoff               = s_fade.pending_panel_off;
        s_fade.pending_panel_off = false;
        (void)esp_timer_stop(s_fade_timer);
    }
    portEXIT_CRITICAL(&s_fade_mux);

    /* Hardware writes outside the lock. A brightness write that loses
     * the race with a preempting start_fade() is benign — the next
     * ~33 ms tick of the new fade overwrites it. DISPOFF gets one more
     * generation re-check immediately before it fires so a wake event
     * arriving between Pass 2 and here can still cancel the panel-off. */
    amoled_co5300_175_set_brightness_pct(to_write);
    if (do_dispoff) {
        portENTER_CRITICAL(&s_fade_mux);
        const bool still_ours = (s_fade.generation == my_gen);
        portEXIT_CRITICAL(&s_fade_mux);
        if (still_ours) {
            amoled_co5300_175_set_display_on(false);
        }
    }
}

static void start_fade(uint8_t target_pct, int64_t duration_us, bool pending_off)
{
    bool short_circuit;
    bool need_start         = false;
    bool brightness_changed = false;

    portENTER_CRITICAL(&s_fade_mux);
    /* Short-circuit when the ramp would have zero duration *or* zero
     * delta. The zero-delta case matters for the
     * DIMMED_BRIGHTNESS_PCT=0 config: DIMMED→OFF would otherwise
     * spend SLEEP_FADE_MS writing 0→0 before issuing the DISPOFF,
     * delaying the panel-off by the full fade duration for no
     * visible benefit. */
    const uint8_t old_current = s_fade.current_pct;
    short_circuit = (duration_us <= 0) || (old_current == target_pct);
    if (short_circuit) {
        brightness_changed       = (old_current != target_pct);
        s_fade.current_pct       = target_pct;
        s_fade.active            = false;
        s_fade.pending_panel_off = false;
        /* Bump generation so any in-flight fade_step_cb that already
         * snapshot the previous fade's parameters will recognise
         * itself as stale and skip its hardware writes. */
        s_fade.generation++;
    } else {
        /* Anchor the new ramp at the current interpolated value so a
         * preempting event blends from wherever the in-flight fade
         * had reached — no visible jump back to the previous start. */
        s_fade.start_pct         = old_current;
        s_fade.target_pct        = target_pct;
        s_fade.start_us          = esp_timer_get_time();
        s_fade.duration_us       = duration_us;
        /* Overwrite, don't OR: a wake-direction fade must clear any
         * pending DISPOFF left behind by a preempted sleep fade. */
        s_fade.pending_panel_off = pending_off;
        s_fade.generation++;
        need_start               = !s_fade.active;
        s_fade.active            = true;
    }
    portEXIT_CRITICAL(&s_fade_mux);

    if (short_circuit) {
        (void)esp_timer_stop(s_fade_timer);
        /* Only write 0x51 when the value would actually change —
         * skips a redundant QSPI command in the legacy snap path
         * (duration=0) when the SM's target already matches the
         * current brightness (e.g. transitions that change only
         * `panel_on`, not `brightness_pct`). */
        if (brightness_changed) {
            amoled_co5300_175_set_brightness_pct(target_pct);
        }
        if (pending_off) {
            amoled_co5300_175_set_display_on(false);
        }
        return;
    }

    if (need_start) {
        (void)esp_timer_start_periodic(s_fade_timer, FADE_STEP_PERIOD_US);
    }
}

static void apply_output(const burn_idle_output_t *out)
{
    const bool waking   = out->panel_on && !s_prev_panel_on;
    const bool sleeping = !out->panel_on && s_prev_panel_on;

    if (waking) {
        /* Wake-first: DISPON is instantaneous so the ramp's brightness
         * writes actually reach the panel. The ramp itself goes from
         * the current 0x51 value (0 after a prior sleep-last) up to
         * the SM's target. */
        amoled_co5300_175_set_display_on(true);
        start_fade(out->brightness_pct,
                   (int64_t)CONFIG_BURNSCOPE_AMOLED_WAKE_FADE_MS * 1000LL,
                   /*pending_panel_off=*/false);
    } else if (sleeping) {
        /* Sleep-last: ramp down to 0, then DISPOFF inside the fade
         * callback once the target is reached. */
        start_fade(/*target_pct=*/0,
                   (int64_t)CONFIG_BURNSCOPE_AMOLED_SLEEP_FADE_MS * 1000LL,
                   /*pending_panel_off=*/true);
    } else {
        /* Same-power transition (e.g. ACTIVE↔DIMMED). Direction picks
         * the duration so a snappy wake-up to brighter still feels
         * snappy when promoted from DIMMED rather than from OFF. */
        uint8_t current;
        portENTER_CRITICAL(&s_fade_mux);
        current = s_fade.current_pct;
        portEXIT_CRITICAL(&s_fade_mux);
        const bool going_up = out->brightness_pct > current;
        const int64_t duration_us = going_up
            ? (int64_t)CONFIG_BURNSCOPE_AMOLED_WAKE_FADE_MS  * 1000LL
            : (int64_t)CONFIG_BURNSCOPE_AMOLED_SLEEP_FADE_MS * 1000LL;
        start_fade(out->brightness_pct, duration_us,
                   /*pending_panel_off=*/false);
    }
    s_prev_panel_on = out->panel_on;
}

static void drain_task(void *arg)
{
    (void)arg;
    burn_idle_event_t ev;
    for (;;) {
        if (xQueueReceive(s_queue, &ev, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        const int64_t now = esp_timer_get_time();
        const burn_idle_output_t out = burn_idle_step(&s_sm, ev, now);
        if (out.changed) {
            ESP_LOGD(TAG, "ev=%d → state=%d br=%u panel_on=%d",
                     (int)ev, (int)out.state, (unsigned)out.brightness_pct,
                     (int)out.panel_on);
            apply_output(&out);
        }
    }
}

static void install_button_isr(void)
{
    /* Configure the pin ourselves. factory_reset.c also configures it
     * (and sets intr_type=NEGEDGE to stay compatible with us), but its
     * config runs inside a FreeRTOS task that may not have been
     * scheduled by the time we install the ISR — so we set the
     * canonical config here too. The two configurations are identical,
     * so whichever runs last is a no-op. */
    const gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << BUTTON_GPIO,
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .intr_type    = GPIO_INTR_NEGEDGE,
    };
    ESP_ERROR_CHECK(gpio_config(&cfg));

    /* gpio_install_isr_service is process-wide; a second install
     * returns ESP_ERR_INVALID_STATE which is fine for our purposes. */
    esp_err_t err = gpio_install_isr_service(0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "gpio_install_isr_service: %s", esp_err_to_name(err));
        return;
    }
    ESP_ERROR_CHECK(gpio_isr_handler_add(BUTTON_GPIO, button_isr, NULL));
}

void burn_idle_adapter_start(void)
{
    if (s_started) {
        return;
    }

    const burn_idle_config_t cfg = {
        .dim_after_us
            = (int64_t)CONFIG_BURNSCOPE_AMOLED_IDLE_DIM_MINUTES * 60LL * 1000000LL,
        .off_after_us
            = (int64_t)CONFIG_BURNSCOPE_AMOLED_IDLE_OFF_MINUTES * 60LL * 1000000LL,
        .active_brightness_pct = CONFIG_BURNSCOPE_AMOLED_ACTIVE_BRIGHTNESS_PCT,
        .dimmed_brightness_pct = CONFIG_BURNSCOPE_AMOLED_DIMMED_BRIGHTNESS_PCT,
        /* No IMU on this board — motion wake is not a source. The SM
         * ignores this field (it's carried for the adapter's sampler,
         * which we don't run); 0 keeps burn_idle_config_valid happy. */
        .motion_threshold_mg   = 0,
    };
    if (!burn_idle_config_valid(&cfg)) {
        ESP_LOGE(TAG, "invalid burn-in config — adapter not started");
        return;
    }
    burn_idle_init(&s_sm, cfg);

    /* Sync the panel to the SM's ACTIVE baseline before timers/ISRs fire.
     * The driver's boot sequence sets brightness to a hard-coded constant
     * (CO5300_DEFAULT_BRIGHTNESS ≈ 70 %) that matches the Kconfig default
     * by design. If a deployment tunes BURNSCOPE_AMOLED_ACTIVE_BRIGHTNESS_PCT
     * to a different value, the SM alone would never write the brightness
     * register at startup — so write it explicitly here.
     * s_prev_panel_on is already true (matches the panel's post-init
     * state), so apply_output's wake-first guard stays consistent. */
    amoled_co5300_175_set_display_on(true);
    amoled_co5300_175_set_brightness_pct(cfg.active_brightness_pct);
    /* Seed the fade engine with the actual brightness we just wrote so
     * the first subsequent ramp starts from the correct anchor. Without
     * this, the first ACTIVE→DIMMED transition would anchor the ramp at
     * 0 and the first fade tick would write 0 — a visible jump to black
     * before ramping back up toward the dimmed target. */
    s_fade.current_pct = cfg.active_brightness_pct;

    s_queue = xQueueCreate(EVENT_QUEUE_DEPTH, sizeof(burn_idle_event_t));
    configASSERT(s_queue != NULL);

    /* The snapshot store may not have been initialised yet — main.c's
     * sequence puts display_profile_init (our caller) before
     * snapshot_store_init. The init is idempotent and cheap; calling
     * it here decouples adapter bring-up from main.c ordering. */
    snapshot_store_init();
    snapshot_store_register_listener(on_snapshot_push, NULL);

    install_button_isr();

    const esp_timer_create_args_t tick_args = {
        .callback        = tick_timer_cb,
        .arg             = NULL,
        .dispatch_method = ESP_TIMER_TASK,
        .name            = "burn_idle_tick",
    };
    ESP_ERROR_CHECK(esp_timer_create(&tick_args, &s_tick_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(s_tick_timer, TICK_PERIOD_US));

    /* Created but not started — start_fade() starts the periodic timer
     * on demand and fade_step_cb() stops it on completion, so it only
     * draws CPU while a ramp is in flight. */
    const esp_timer_create_args_t fade_args = {
        .callback        = fade_step_cb,
        .arg             = NULL,
        .dispatch_method = ESP_TIMER_TASK,
        .name            = "burn_idle_fade",
    };
    ESP_ERROR_CHECK(esp_timer_create(&fade_args, &s_fade_timer));

    BaseType_t ok = xTaskCreate(drain_task, "burn_idle_drain",
                                DRAIN_TASK_STACK, NULL,
                                DRAIN_TASK_PRIORITY, NULL);
    configASSERT(ok == pdPASS);

    s_started = true;
    ESP_LOGI(TAG,
             "started: dim=%d min, off=%d min, br=%d%%/%d%%, "
             "fade wake=%d ms / sleep=%d ms (no IMU/touch)",
             CONFIG_BURNSCOPE_AMOLED_IDLE_DIM_MINUTES,
             CONFIG_BURNSCOPE_AMOLED_IDLE_OFF_MINUTES,
             CONFIG_BURNSCOPE_AMOLED_ACTIVE_BRIGHTNESS_PCT,
             CONFIG_BURNSCOPE_AMOLED_DIMMED_BRIGHTNESS_PCT,
             CONFIG_BURNSCOPE_AMOLED_WAKE_FADE_MS,
             CONFIG_BURNSCOPE_AMOLED_SLEEP_FADE_MS);
}
