#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/*
 * nvs_store.h — narrow wrapper over `nvs_flash` for the things we persist:
 *   - WiFi credentials captured by the captive portal.
 *   - Per-agent pairing identifier (`X-BurnScope-Client-Id`) used for
 *     trust-on-first-use authorisation in `POST /summary` / `GET /health`.
 *
 * The wrapper deliberately does NOT expose generic nvs_get/set helpers —
 * we want every persisted byte to go through a typed accessor so we can
 * audit logging (FR-6.2: password must never be echoed).
 */

/*
 * Cap on stored client-id length. Matches the wire-format limit
 * (RFC 5321 mailbox: 254 bytes + NUL).
 */
#define BURNSCOPE_CLIENT_ID_MAX 255

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

/**
 * Load the bound client-id for `agent` (one of "claude", "codex") into
 * `out`. `cap` must be at least `BURNSCOPE_CLIENT_ID_MAX`. Returns
 * `ESP_ERR_NVS_NOT_FOUND` when the slot is empty (TOFU: the next request
 * will claim it). On any other failure returns the underlying error.
 */
esp_err_t nvs_store_load_client_id(const char *agent, char *out, size_t cap);

/**
 * Persist the client-id bound to `agent`. Skip-write-when-equal, same
 * defence as `nvs_store_save_creds`. `id` must be NUL-terminated and ≤
 * `BURNSCOPE_CLIENT_ID_MAX - 1` bytes.
 */
esp_err_t nvs_store_save_client_id(const char *agent, const char *id);

/**
 * Erase every per-agent client-id slot in NVS. Called from the AP-mode
 * reprovision path so a re-flashed / re-provisioned device can be
 * claimed by a fresh owner.
 */
esp_err_t nvs_store_erase_client_ids(void);

/**
 * Load the cached LCD silicon ID (RDID1 byte) into `*out`. Returns
 * `ESP_ERR_NVS_NOT_FOUND` when the slot is empty (first boot — the
 * caller bit-bangs the ID and persists it via `nvs_store_save_lcd_id`).
 * This is a non-credential cache, but routing it through `nvs_store`
 * keeps every persisted byte under one auditable wrapper.
 */
esp_err_t nvs_store_load_lcd_id(uint8_t *out);

/**
 * Persist the LCD silicon ID. Skip-write-when-equal, same defence as
 * `nvs_store_save_creds`, so a hardware that re-reads RDID1 on every
 * boot does not wear the NVS partition.
 */
esp_err_t nvs_store_save_lcd_id(uint8_t id);
