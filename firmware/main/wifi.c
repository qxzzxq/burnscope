/*
 * WiFi: STA from runtime-supplied creds, or open AP for the captive-portal
 * provisioning flow. Exponential-backoff STA reconnect preserved from
 * Phase 1.
 *
 * Phase 3 / FR-1.6: after AUTH_FAIL_FALLBACK_THRESHOLD consecutive
 * auth-flavoured disconnects without an intervening successful association,
 * we wipe the stored creds and restart so the empty-NVS boot path lands in
 * captive-portal provisioning. EC-CP-200 covers the password-changed-
 * upstream case. Non-auth disconnects (NO_AP_FOUND, association drops, etc.)
 * stay on the exponential-backoff reconnect path so a brief AP outage or
 * range-walk doesn't blow away credentials.
 */

#include "wifi.h"

#include <string.h>

#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/timers.h"

#include "nvs_store.h"

static const char *TAG = "wifi";

#define AUTH_FAIL_FALLBACK_THRESHOLD 5

static wifi_state_cb_t s_state_cb = NULL;
static esp_netif_t    *s_sta_netif = NULL;
static esp_netif_t    *s_ap_netif  = NULL;
static TimerHandle_t   s_reconnect_timer = NULL;
static int             s_backoff_s = 1;
static int             s_auth_fail_streak = 0;
static bool            s_had_ip = false;
static bool            s_ap_mode = false;

static void notify(wifi_state_t state)
{
    if (s_state_cb != NULL) {
        s_state_cb(state);
    }
}

static int next_backoff(int current)
{
    int next = current * 2;
    if (next > 30) {
        next = 30;
    }
    return next;
}

static void reconnect_timer_cb(TimerHandle_t t)
{
    (void)t;
    ESP_LOGI(TAG, "reconnect attempt (next backoff %ds)", s_backoff_s);
    esp_err_t err = esp_wifi_connect();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "esp_wifi_connect failed: %s", esp_err_to_name(err));
    }
}

static void schedule_reconnect(void)
{
    if (s_reconnect_timer == NULL) {
        return;
    }
    TickType_t delay = pdMS_TO_TICKS(s_backoff_s * 1000);
    xTimerChangePeriod(s_reconnect_timer, delay, 0);
    xTimerStart(s_reconnect_timer, 0);
    s_backoff_s = next_backoff(s_backoff_s);
}

static bool is_auth_failure_reason(uint8_t reason)
{
    /* Reasons that imply the stored credentials are no longer valid for
     * this AP (password changed, key handshake failed). NO_AP_FOUND and
     * association-drop reasons are deliberately excluded so a transient
     * AP outage or range-walk doesn't wipe creds (FR-1.6 talks about
     * *persistent auth* failure, not any disconnect). */
    switch (reason) {
    case WIFI_REASON_AUTH_EXPIRE:
    case WIFI_REASON_AUTH_FAIL:
    case WIFI_REASON_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
        return true;
    default:
        return false;
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT) {
        switch (id) {
        case WIFI_EVENT_STA_START:
            if (s_ap_mode) {
                /* APSTA is active only so the captive portal can scan;
                 * the STA radio must not auto-associate. */
                break;
            }
            ESP_LOGI(TAG, "STA start");
            notify(WIFI_STATE_CONNECTING);
            ESP_ERROR_CHECK(esp_wifi_connect());
            break;
        case WIFI_EVENT_STA_CONNECTED:
            if (s_ap_mode) break;
            ESP_LOGI(TAG, "STA connected (awaiting IP)");
            break;
        case WIFI_EVENT_STA_DISCONNECTED: {
            if (s_ap_mode) {
                /* During the AP scan we briefly enter APSTA — ignore. */
                break;
            }
            const wifi_event_sta_disconnected_t *evt =
                (const wifi_event_sta_disconnected_t *)data;
            const uint8_t reason = evt ? evt->reason : 0;
            if (is_auth_failure_reason(reason)) {
                s_auth_fail_streak++;
                ESP_LOGW(TAG, "STA auth failure (reason=%u, streak=%d/%d)",
                         reason, s_auth_fail_streak,
                         AUTH_FAIL_FALLBACK_THRESHOLD);
                if (s_auth_fail_streak >= AUTH_FAIL_FALLBACK_THRESHOLD) {
                    ESP_LOGW(TAG,
                             "%d consecutive auth failures — wiping creds "
                             "and restarting into AP mode",
                             s_auth_fail_streak);
                    (void)nvs_store_erase_creds();
                    esp_restart();
                    /* esp_restart() does not return; the break below is
                     * just to keep the compiler happy. */
                    break;
                }
            }
            ESP_LOGW(TAG, "STA disconnected (reason=%u); scheduling reconnect in %ds",
                     reason, s_backoff_s);
            notify(s_had_ip ? WIFI_STATE_RECONNECTING : WIFI_STATE_DISCONNECTED);
            schedule_reconnect();
            break;
        }
        case WIFI_EVENT_AP_START:
            ESP_LOGI(TAG, "AP started");
            notify(WIFI_STATE_AP_MODE);
            break;
        default:
            break;
        }
    } else if (base == IP_EVENT) {
        if (id == IP_EVENT_STA_GOT_IP) {
            ip_event_got_ip_t *evt = (ip_event_got_ip_t *)data;
            ESP_LOGI(TAG, "got IP " IPSTR, IP2STR(&evt->ip_info.ip));
            s_backoff_s = 1;
            s_auth_fail_streak = 0;
            s_had_ip = true;
            notify(WIFI_STATE_GOT_IP);
        }
    }
}

void wifi_init(void)
{
    static bool inited = false;
    if (inited) {
        return;
    }

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    /* Keep esp_wifi's own config in RAM. Otherwise IDF silently caches
     * the last `wifi_config_t` in its `nvs.net80211` namespace and the
     * STA radio auto-reconnects to a stale SSID on the next boot — which
     * both bypasses our captive-portal flow and violates FR-6.1
     * ("WiFi creds shall be the only values persisted to NVS"). Our
     * `burnscope_wifi` namespace is the only persistent store. */
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, NULL));

    s_reconnect_timer = xTimerCreate("wifi_rc", pdMS_TO_TICKS(1000),
                                     pdFALSE, NULL, reconnect_timer_cb);
    configASSERT(s_reconnect_timer != NULL);

    inited = true;
}

void wifi_register_state_cb(wifi_state_cb_t cb)
{
    s_state_cb = cb;
}

void wifi_start_sta(const wifi_creds_t *creds)
{
    configASSERT(creds != NULL);

    if (s_sta_netif == NULL) {
        s_sta_netif = esp_netif_create_default_wifi_sta();
        configASSERT(s_sta_netif != NULL);
    }

    wifi_config_t wcfg = { 0 };
    strncpy((char *)wcfg.sta.ssid, creds->ssid, sizeof(wcfg.sta.ssid) - 1);
    strncpy((char *)wcfg.sta.password, creds->password, sizeof(wcfg.sta.password) - 1);
    wcfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wcfg));

    /* FR-6.2: log SSID only, never the password. */
    ESP_LOGI(TAG, "connecting STA to SSID '%s'", creds->ssid);
    ESP_ERROR_CHECK(esp_wifi_start());
}

void wifi_start_ap(const char *ssid)
{
    configASSERT(ssid != NULL && ssid[0] != '\0');

    if (s_ap_netif == NULL) {
        s_ap_netif = esp_netif_create_default_wifi_ap();
        configASSERT(s_ap_netif != NULL);
    }

    wifi_config_t wcfg = { 0 };
    strncpy((char *)wcfg.ap.ssid, ssid, sizeof(wcfg.ap.ssid) - 1);
    wcfg.ap.ssid_len = strlen((char *)wcfg.ap.ssid);
    wcfg.ap.channel = 1;
    wcfg.ap.authmode = WIFI_AUTH_OPEN;
    wcfg.ap.max_connection = 4;
    wcfg.ap.beacon_interval = 100;

    /* APSTA so we can also run scans for the captive portal's network
     * picker (TC-CP-102). */
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &wcfg));
    ESP_LOGI(TAG, "starting AP '%s' (open, ch %d)", ssid, wcfg.ap.channel);
    s_ap_mode = true;
    ESP_ERROR_CHECK(esp_wifi_start());
}
