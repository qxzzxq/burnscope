#pragma once

#include "esp_netif.h"

#include "nvs_store.h"

/*
 * wifi.h — runtime-configurable WiFi: STA from caller-supplied creds, or
 * open AP for captive-portal provisioning. Exponential-backoff reconnect
 * is preserved from Phase 1.
 *
 * Typical sequence:
 *   wifi_init();                           // once at boot
 *   wifi_register_state_cb(cb);            // optional
 *   if (have creds in NVS) wifi_start_sta(&creds);
 *   else                   wifi_start_ap("BURNSCOPE-XXXX");
 */

typedef enum {
    WIFI_STATE_CONNECTING,
    WIFI_STATE_RECONNECTING,
    WIFI_STATE_GOT_IP,
    WIFI_STATE_DISCONNECTED,
    WIFI_STATE_AP_MODE,
} wifi_state_t;

typedef void (*wifi_state_cb_t)(wifi_state_t state);

/**
 * Initialise TCP/IP stack + event loop + esp_wifi core. Does NOT pick a
 * mode or programme any SSID — the subsequent `wifi_start_*` call does
 * that. Idempotent.
 */
void wifi_init(void);

/**
 * Register a state-change callback. Invoked from the default event loop
 * task. NULL clears the callback.
 */
void wifi_register_state_cb(wifi_state_cb_t cb);

/**
 * Start station mode with the supplied credentials. Triggers
 * WIFI_STATE_CONNECTING immediately; later transitions arrive via the
 * registered callback.
 */
void wifi_start_sta(const wifi_creds_t *creds);

/**
 * Bring up an open SoftAP advertising `ssid` on channel 1, 192.168.4.0/24.
 * Used by the captive-portal flow. Issues WIFI_STATE_AP_MODE.
 */
void wifi_start_ap(const char *ssid);
