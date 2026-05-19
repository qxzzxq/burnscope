/*
 * BurnScope firmware — Phase 1 orchestrator.
 *
 * Boots the panel, brings up WiFi STA from compile-time credentials, and
 * starts mDNS / SNTP / HTTP once the device has an IP. UI mirrors the
 * WiFi state so the user can see what's happening from across the room.
 */

#include "esp_err.h"
#include "esp_log.h"
#include "nvs_flash.h"

#include "http_server.h"
#include "mdns_svc.h"
#include "ntp.h"
#include "panel.h"
#include "ui.h"
#include "version.h"
#include "watchdog.h"
#include "wifi.h"

static const char *TAG = "burnscope";

static void on_wifi_state(wifi_state_t state)
{
    switch (state) {
    case WIFI_STATE_CONNECTING:
        ui_set_status("Connecting...");
        break;
    case WIFI_STATE_RECONNECTING:
        ui_set_status("Reconnecting...");
        break;
    case WIFI_STATE_DISCONNECTED:
        ui_set_status("WiFi unavailable");
        break;
    case WIFI_STATE_GOT_IP:
        mdns_svc_start();
        ntp_start();
        http_server_start();
        ui_set_status("Waiting for daemon...");
        break;
    }
}

static void init_nvs(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW("nvs", "NVS needs erase (%s); reinitialising", esp_err_to_name(err));
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
}

void app_main(void)
{
    ESP_LOGI(TAG, "BurnScope firmware_version=%s starting", BURNSCOPE_FW_VERSION);

    init_nvs();

    lv_display_t *disp = panel_init();
    ui_init(disp);
    ui_set_status("Connecting...");

    watchdog_start();

    wifi_init();
    wifi_register_state_cb(on_wifi_state);
    wifi_start();
}
