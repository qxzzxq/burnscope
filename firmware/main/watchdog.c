#include "watchdog.h"

#include "esp_err.h"
#include "esp_log.h"
#include "esp_task_wdt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "wdt";

static bool s_started = false;

static void heartbeat_task(void *arg)
{
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    ESP_LOGI(TAG, "heartbeat task subscribed to TWDT");

#ifdef BURNSCOPE_WDT_INJECT_HANG
    /* TC-WDT-100: stop feeding the WDT after 5 s; device should reboot
       within the TWDT timeout (~10 s). */
    for (int i = 0; i < 5; i++) {
        esp_task_wdt_reset();
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    ESP_LOGW(TAG, "BURNSCOPE_WDT_INJECT_HANG set; ceasing heartbeats");
    vTaskDelay(portMAX_DELAY);
#else
    for (;;) {
        esp_task_wdt_reset();
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
#endif
}

void watchdog_start(void)
{
    if (s_started) {
        return;
    }
    s_started = true;
    BaseType_t ok = xTaskCreate(heartbeat_task, "wdt_hb", 2048, NULL, 4, NULL);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "failed to spawn heartbeat task");
    }
}
