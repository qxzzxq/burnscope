/*
 * AMOLED idle adapter — see burn_idle_adapter.h for the role and event
 * topology. Implementation notes:
 *
 *  - Brightness/sleep changes apply only when the SM reports
 *    `output.changed`; ordering is wake-first (panel-on before
 *    brightness write) and sleep-last (brightness 0 then panel-off) so
 *    a transitioning frame never flashes black before the panel sleeps
 *    or full-bright before a wake.
 *  - The 1 Hz tick is driven by an `esp_timer_*` periodic callback that
 *    runs on the esp_timer task and posts EV_TIME into the queue. We do
 *    *not* call `burn_idle_step` directly from the timer callback —
 *    the queue serialises across producers so the SM stays
 *    single-threaded as its purity contract requires.
 *  - Button ISR debounce is in-ISR via `esp_timer_get_time()`: cheap
 *    on the S3 (~µs) and avoids a software timer per press.
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

static burn_idle_t        s_sm;
static QueueHandle_t      s_queue       = NULL;
static esp_timer_handle_t s_tick_timer  = NULL;
static volatile int64_t   s_last_button_us = 0;
static bool               s_prev_panel_on = true;  /* matches SM init baseline */
static bool               s_started        = false;

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

void burn_idle_adapter_notify_touch(void)
{
    post_event(BURN_IDLE_EV_TOUCH);
}

static void apply_output(const burn_idle_output_t *out)
{
    /* Wake-first: bring the panel back before changing brightness so
     * the first rendered frame after wake isn't preceded by a
     * brightness write that the panel ignores (or worse, races). */
    if (out->panel_on && !s_prev_panel_on) {
        amoled_sh8601_set_display_on(true);
    }
    amoled_sh8601_set_brightness_pct(out->brightness_pct);
    if (!out->panel_on && s_prev_panel_on) {
        amoled_sh8601_set_display_on(false);
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
     * (and now sets intr_type=NEGEDGE to stay compatible with us), but
     * its config runs inside a FreeRTOS task that may not have been
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
        .motion_threshold_mg   = CONFIG_BURNSCOPE_AMOLED_MOTION_THRESHOLD_MG,
    };
    if (!burn_idle_config_valid(&cfg)) {
        ESP_LOGE(TAG, "invalid burn-in config — adapter not started");
        return;
    }
    burn_idle_init(&s_sm, cfg);

    /* Sync the panel to the SM's ACTIVE baseline before timers/ISRs fire.
     * The driver's boot sequence sets brightness to a hard-coded constant
     * (currently SH8601_DEFAULT_BRIGHTNESS = 0xB2, ≈ 70 %) that matches
     * the Kconfig default by design. If a deployment tunes
     * BURNSCOPE_AMOLED_ACTIVE_BRIGHTNESS_PCT to a different value, the SM
     * alone would never write the brightness register at startup —
     * subsequent EV_TIME ticks return changed=false while the SM stays in
     * ACTIVE, so the panel would inherit 0xB2 until the first
     * dim/off/wake transition.  s_prev_panel_on is already true (matches
     * the panel's post-init state), so apply_output's wake-first guard
     * stays consistent. */
    amoled_sh8601_set_display_on(true);
    amoled_sh8601_set_brightness_pct(cfg.active_brightness_pct);

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

    BaseType_t ok = xTaskCreate(drain_task, "burn_idle_drain",
                                DRAIN_TASK_STACK, NULL,
                                DRAIN_TASK_PRIORITY, NULL);
    configASSERT(ok == pdPASS);

    s_started = true;
    ESP_LOGI(TAG,
             "started: dim=%d min, off=%d min, br=%d%%/%d%%, motion=%d mg",
             CONFIG_BURNSCOPE_AMOLED_IDLE_DIM_MINUTES,
             CONFIG_BURNSCOPE_AMOLED_IDLE_OFF_MINUTES,
             CONFIG_BURNSCOPE_AMOLED_ACTIVE_BRIGHTNESS_PCT,
             CONFIG_BURNSCOPE_AMOLED_DIMMED_BRIGHTNESS_PCT,
             CONFIG_BURNSCOPE_AMOLED_MOTION_THRESHOLD_MG);
}
