#pragma once

#include <stdbool.h>

#include "esp_err.h"

/*
 * nvs_store.h — narrow wrapper over `nvs_flash` for the one thing we store
 * in MVP: WiFi credentials captured by the captive portal.
 *
 * The wrapper deliberately does NOT expose generic nvs_get/set helpers —
 * we want every persisted byte to go through a typed accessor so we can
 * audit logging (FR-6.2: password must never be echoed).
 */

typedef struct {
    char ssid[33];        /* WiFi 802.11 SSID: 32 bytes + NUL */
    char password[65];    /* WPA2/WPA3 passphrase: 64 bytes + NUL */
} wifi_creds_t;

/**
 * Read credentials from NVS into `*out`. Returns ESP_ERR_NVS_NOT_FOUND if
 * either field is missing (treat as "device not provisioned yet").
 */
esp_err_t nvs_store_load_creds(wifi_creds_t *out);

/**
 * Persist credentials. Skips the write+commit when the bytes already
 * match what's stored — defends against accidental write storms ever
 * filling the NVS partition (FSD §5.1 risk).
 */
esp_err_t nvs_store_save_creds(const wifi_creds_t *in);

/**
 * Wipe the stored credentials. Used by factory-reset paths.
 */
esp_err_t nvs_store_erase_creds(void);

/**
 * Convenience: true when both SSID and password are populated in NVS.
 */
bool nvs_store_has_creds(void);
