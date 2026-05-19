/*
 * WiFi STA with exponential-backoff reconnect.
 *
 * Phase 1 only — credentials are compile-time. TODO(phase2): swap for
 * NVS-backed creds and add AP-fallback after N consecutive auth failures
 * (FR-1.6).
 */

#include "wifi.h"

#include <string.h>

#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/timers.h"
#include "nvs_flash.h"

#include "wifi_creds.h"

static const char *TAG = "wifi";

static wifi_state_cb_t s_state_cb = NULL;
static esp_netif_t    *s_sta_netif = NULL;
static TimerHandle_t   s_reconnect_timer = NULL;
static int             s_backoff_s = 1;
static bool            s_had_ip = false;

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

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    (void)arg; (void)data;
    if (base == WIFI_EVENT) {
        switch (id) {
        case WIFI_EVENT_STA_START:
            ESP_LOGI(TAG, "STA start; connecting to SSID '%s'", BURNSCOPE_WIFI_SSID);
            notify(WIFI_STATE_CONNECTING);
            ESP_ERROR_CHECK(esp_wifi_connect());
            break;
        case WIFI_EVENT_STA_CONNECTED:
            ESP_LOGI(TAG, "STA connected (awaiting IP)");
            break;
        case WIFI_EVENT_STA_DISCONNECTED:
            ESP_LOGW(TAG, "STA disconnected; scheduling reconnect in %ds", s_backoff_s);
            notify(s_had_ip ? WIFI_STATE_RECONNECTING : WIFI_STATE_DISCONNECTED);
            schedule_reconnect();
            break;
        default:
            break;
        }
    } else if (base == IP_EVENT) {
        if (id == IP_EVENT_STA_GOT_IP) {
            ip_event_got_ip_t *evt = (ip_event_got_ip_t *)data;
            ESP_LOGI(TAG, "got IP " IPSTR, IP2STR(&evt->ip_info.ip));
            s_backoff_s = 1;
            s_had_ip = true;
            notify(WIFI_STATE_GOT_IP);
        }
    }
}

esp_netif_t *wifi_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    s_sta_netif = esp_netif_create_default_wifi_sta();
    assert(s_sta_netif != NULL);

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, NULL));

    wifi_config_t wcfg = { 0 };
    strncpy((char *)wcfg.sta.ssid, BURNSCOPE_WIFI_SSID, sizeof(wcfg.sta.ssid) - 1);
    strncpy((char *)wcfg.sta.password, BURNSCOPE_WIFI_PASSWORD, sizeof(wcfg.sta.password) - 1);
    wcfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wcfg));

    s_reconnect_timer = xTimerCreate("wifi_rc", pdMS_TO_TICKS(1000),
                                     pdFALSE, NULL, reconnect_timer_cb);
    assert(s_reconnect_timer != NULL);

    return s_sta_netif;
}

void wifi_register_state_cb(wifi_state_cb_t cb)
{
    s_state_cb = cb;
}

void wifi_start(void)
{
    ESP_ERROR_CHECK(esp_wifi_start());
}
