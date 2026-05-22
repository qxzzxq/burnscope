/*
 * BurnScope firmware — Phase 2 orchestrator.
 *
 * Boots the panel + UI, decides between STA-from-NVS and captive-portal
 * AP mode, and brings up mDNS / SNTP / HTTP once an IP is acquired. The
 * snapshot listener routes incoming pushes to the display profile.
 */

#include "esp_err.h"
#include "esp_log.h"
#include "nvs_flash.h"

#include "display_profile.h"
#include "factory_reset.h"
#include "http_server.h"
#include "mdns_svc.h"
#include "ntp.h"
#include "nvs_store.h"
#include "provisioning.h"
#include "snapshot.h"
#include "version.h"
#include "watchdog.h"
#include "wifi.h"

static const char *TAG = "burnscope";

static void on_snapshot(const agent_snapshot_t *snap, void *user)
{
    (void)user;
    display_profile_show_agent(snap);
}

/* Helper for restoring the agent view after a reconnect: grab the first
 * occupied slot from the snapshot store. The 1 Hz UI tick takes care of
 * cycling between agents from there. */
typedef struct {
    agent_snapshot_t snap;
    bool             found;
} first_snap_t;

static void grab_first_snapshot(const agent_snapshot_t *snap, void *user)
{
    first_snap_t *out = (first_snap_t *)user;
    if (!out->found) {
        out->snap  = *snap;
        out->found = true;
    }
}

static void on_wifi_state(wifi_state_t state)
{
    switch (state) {
    case WIFI_STATE_CONNECTING:
        display_profile_show_status("Connecting...");
        break;
    case WIFI_STATE_RECONNECTING:
        display_profile_show_status("Reconnecting...");
        break;
    case WIFI_STATE_DISCONNECTED:
        display_profile_show_status("WiFi unavailable");
        break;
    case WIFI_STATE_GOT_IP:
        mdns_svc_start();
        ntp_start();
        http_server_start();
        /* Restore whichever view makes sense for the moment:
         *   - Fresh boot, no data yet: "Waiting for daemon..."
         *   - Reconnect after a blip with snapshots still in RAM: jump
         *     straight back to the agent view instead of leaving the
         *     "Reconnecting..." splash up until the next push lands. */
        if (snapshot_store_count() == 0) {
            display_profile_show_status("Waiting for daemon...");
        } else {
            first_snap_t fs = { .found = false };
            snapshot_store_foreach(grab_first_snapshot, &fs);
            if (fs.found) {
                display_profile_show_agent(&fs.snap);
            }
        }
        break;
    case WIFI_STATE_AP_MODE:
        /* Splash is set explicitly by the AP path below. */
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
    display_profile_init();
    display_profile_show_status("Booting...");

    snapshot_store_init();
    snapshot_store_register_listener(on_snapshot, NULL);

    watchdog_start();
    factory_reset_start();

    wifi_init();
    wifi_register_state_cb(on_wifi_state);

    wifi_creds_t creds;
    if (nvs_store_load_creds(&creds) == ESP_OK) {
        display_profile_show_status("Connecting...");
        wifi_start_sta(&creds);
    } else {
        /* provisioning_start paints the splash with the real SSID. */
        provisioning_start();
    }
}
