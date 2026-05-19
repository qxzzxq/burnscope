#include "ntp.h"

#include "esp_err.h"
#include "esp_log.h"
#include "esp_netif_sntp.h"

static const char *TAG = "ntp";

static bool s_started = false;

void ntp_start(void)
{
    if (s_started) {
        return;
    }

    esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    cfg.start = true;
    ESP_ERROR_CHECK(esp_netif_sntp_init(&cfg));

    ESP_LOGI(TAG, "SNTP started against pool.ntp.org");
    s_started = true;
}
