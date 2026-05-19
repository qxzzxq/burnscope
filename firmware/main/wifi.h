#pragma once

#include "esp_netif.h"

/*
 * wifi.h — Phase 1 WiFi STA bring-up with exponential-backoff reconnect.
 *
 * Credentials are taken from the gitignored `wifi_creds.h` at compile time
 * (Phase 1 only — Phase 2 reads them from NVS via captive-portal flow).
 *
 * Lifecycle:
 *   wifi_init()                  — one-shot init of netif + esp_wifi
 *   wifi_register_state_cb(cb)   — main subscribes for state changes
 *   wifi_start()                 — kicks off STA connect; reconnects forever
 */

typedef enum {
    WIFI_STATE_CONNECTING,
    WIFI_STATE_RECONNECTING,
    WIFI_STATE_GOT_IP,
    WIFI_STATE_DISCONNECTED,
} wifi_state_t;

typedef void (*wifi_state_cb_t)(wifi_state_t state);

/**
 * Initialise TCP/IP stack, event loop, and esp_wifi (STA mode).
 *
 * Returns the STA netif handle (never NULL — panics on failure).
 */
esp_netif_t *wifi_init(void);

/**
 * Register a state-change callback. The callback is invoked from the
 * default event loop task. NULL clears the callback.
 */
void wifi_register_state_cb(wifi_state_cb_t cb);

/**
 * Start the STA association. Triggers WIFI_STATE_CONNECTING immediately.
 */
void wifi_start(void);
