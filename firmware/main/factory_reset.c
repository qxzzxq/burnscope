/*
 * GPIO0 long-press → factory reset. The CYD shares GPIO0 with the boot
 * strap so the button is normally high; pressing it pulls low. A
 * 100-tick (≈ 5 s at 50 ms polling) hold triggers the wipe.
 */

#include "factory_reset.h"

#include "driver/gpio.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "nvs_store.h"

static const char *TAG = "factory";

#define BUTTON_GPIO       GPIO_NUM_0
#define POLL_MS           50
#define HOLD_REQUIRED_MS  5000
#define HOLD_TICKS        (HOLD_REQUIRED_MS / POLL_MS)

static void task(void *arg)
{
    (void)arg;
    const gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << BUTTON_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&cfg));

    int held = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
        if (gpio_get_level(BUTTON_GPIO) == 0) {
            held++;
            if (held == HOLD_TICKS) {
                ESP_LOGW(TAG, "BOOT held ≥ %d ms — erasing creds and rebooting",
                         HOLD_REQUIRED_MS);
                esp_err_t cred_err = nvs_store_erase_creds();
                /* Also clear the TOFU pairing slots so a new owner can
                 * claim the device after reprovisioning. The next
                 * /summary POST from any laptop will rebind verbatim. */
                esp_err_t pair_err = nvs_store_erase_client_ids();

                if (cred_err != ESP_OK) {
                    /* If creds can't be cleared the AP wouldn't come up
                     * after reboot — the user would just see the same
                     * screen and try again. Reset the hold counter and
                     * leave the device running so they can re-attempt
                     * (or read the serial log for the underlying error). */
                    ESP_LOGE(TAG, "factory reset aborted: erase_creds failed: %s",
                             esp_err_to_name(cred_err));
                    held = 0;
                    continue;
                }
                if (pair_err != ESP_OK) {
                    /* Pairing slots couldn't be cleared. WiFi creds are
                     * gone, so the AP will still come back up — log and
                     * proceed; the new owner sees a 401 mismatch on
                     * their first push if the stale binding survives. */
                    ESP_LOGE(TAG, "factory reset: erase_client_ids failed: %s",
                             esp_err_to_name(pair_err));
                }
                vTaskDelay(pdMS_TO_TICKS(100));
                esp_restart();
            }
        } else {
            held = 0;
        }
    }
}

void factory_reset_start(void)
{
    static bool started = false;
    if (started) return;
    started = true;
    xTaskCreate(task, "factory_reset", 2048, NULL, 3, NULL);
}
