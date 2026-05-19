#include "mdns_svc.h"

#include <stdio.h>

#include "esp_err.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "mdns.h"

#include "version.h"

static const char *TAG = "mdns";

static bool s_started = false;

void mdns_svc_start(void)
{
    if (s_started) {
        return;
    }

    ESP_ERROR_CHECK(mdns_init());

    uint8_t mac[6] = { 0 };
    ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_WIFI_STA));

    char hostname[32];
    snprintf(hostname, sizeof(hostname), "burnscope-%02x%02x", mac[4], mac[5]);
    ESP_ERROR_CHECK(mdns_hostname_set(hostname));
    ESP_ERROR_CHECK(mdns_instance_name_set("BurnScope"));

    mdns_txt_item_t txt[] = {
        { "version", BURNSCOPE_FW_VERSION },
    };
    ESP_ERROR_CHECK(mdns_service_add(NULL, "_burnscope", "_tcp", 80,
                                     txt, sizeof(txt) / sizeof(txt[0])));

    ESP_LOGI(TAG, "advertising %s._burnscope._tcp.local on port 80 (v%s)",
             hostname, BURNSCOPE_FW_VERSION);

    s_started = true;
}
