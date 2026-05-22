#include "nvs_store.h"

#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG = "nvs";

#define NS_WIFI         "burnscope_wifi"
#define KEY_SSID        "ssid"
#define KEY_PASSWORD    "password"

/* Pairing slots live in a separate namespace so an `nvs_erase_all` of the
 * WiFi namespace during reprovision wouldn't touch them — we want to
 * decide explicitly when to wipe the client-ids (and we do, from
 * `factory_reset.c`). */
#define NS_PAIR         "burnscope_pair"

static esp_err_t open_ro(const char *ns, nvs_handle_t *out)
{
    return nvs_open(ns, NVS_READONLY, out);
}

static esp_err_t open_rw(const char *ns, nvs_handle_t *out)
{
    return nvs_open(ns, NVS_READWRITE, out);
}

esp_err_t nvs_store_load_creds(wifi_creds_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t h;
    esp_err_t err = open_ro(NS_WIFI, &h);
    if (err != ESP_OK) {
        return ESP_ERR_NVS_NOT_FOUND;
    }

    size_t ssid_len = sizeof(out->ssid);
    size_t pw_len   = sizeof(out->password);
    memset(out, 0, sizeof(*out));

    esp_err_t e1 = nvs_get_str(h, KEY_SSID,     out->ssid,     &ssid_len);
    esp_err_t e2 = nvs_get_str(h, KEY_PASSWORD, out->password, &pw_len);
    nvs_close(h);

    if (e1 != ESP_OK || e2 != ESP_OK || out->ssid[0] == '\0') {
        return ESP_ERR_NVS_NOT_FOUND;
    }
    return ESP_OK;
}

esp_err_t nvs_store_save_creds(const wifi_creds_t *in)
{
    if (in == NULL || in->ssid[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }

    /* No-op fast path: refuse to rewrite identical creds, so a stuck loop
     * cannot wear the NVS partition (FSD §5.1 risk row). */
    wifi_creds_t cur;
    if (nvs_store_load_creds(&cur) == ESP_OK &&
        strncmp(cur.ssid, in->ssid, sizeof(cur.ssid)) == 0 &&
        strncmp(cur.password, in->password, sizeof(cur.password)) == 0) {
        ESP_LOGI(TAG, "creds unchanged for SSID '%s' — skipping write", in->ssid);
        return ESP_OK;
    }

    nvs_handle_t h;
    esp_err_t err = open_rw(NS_WIFI, &h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open RW failed: %s", esp_err_to_name(err));
        return err;
    }

    err = nvs_set_str(h, KEY_SSID, in->ssid);
    if (err == ESP_OK) {
        err = nvs_set_str(h, KEY_PASSWORD, in->password);
    }
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);

    if (err == ESP_OK) {
        /* FR-6.2: log SSID, never the password. */
        ESP_LOGI(TAG, "saved creds for SSID '%s'", in->ssid);
    } else {
        ESP_LOGE(TAG, "failed to save creds: %s", esp_err_to_name(err));
    }
    return err;
}

esp_err_t nvs_store_erase_creds(void)
{
    nvs_handle_t h;
    esp_err_t err = open_rw(NS_WIFI, &h);
    if (err != ESP_OK) {
        return err;
    }
    /* Erase keys individually rather than nvs_erase_all so we don't nuke
     * unrelated namespaces if any are added later. */
    nvs_erase_key(h, KEY_SSID);
    nvs_erase_key(h, KEY_PASSWORD);
    err = nvs_commit(h);
    nvs_close(h);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "erased WiFi creds");
    }
    return err;
}

bool nvs_store_has_creds(void)
{
    wifi_creds_t tmp;
    return nvs_store_load_creds(&tmp) == ESP_OK;
}

/* --------------------------------------------------------------------- */
/* Per-agent pairing slots                                                */
/* --------------------------------------------------------------------- */

esp_err_t nvs_store_load_client_id(const char *agent, char *out, size_t cap)
{
    if (agent == NULL || agent[0] == '\0' || out == NULL ||
        cap < BURNSCOPE_CLIENT_ID_MAX) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t h;
    esp_err_t err = open_ro(NS_PAIR, &h);
    if (err != ESP_OK) {
        /* Namespace absent before the first save — treat as empty slot. */
        return ESP_ERR_NVS_NOT_FOUND;
    }

    size_t len = cap;
    out[0] = '\0';
    err = nvs_get_str(h, agent, out, &len);
    nvs_close(h);

    if (err != ESP_OK || out[0] == '\0') {
        return ESP_ERR_NVS_NOT_FOUND;
    }
    return ESP_OK;
}

esp_err_t nvs_store_save_client_id(const char *agent, const char *id)
{
    if (agent == NULL || agent[0] == '\0' || id == NULL || id[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }
    /* Defensive bounds: callers should never get this wrong, but
     * `nvs_set_str` would happily write a huge blob if we let it. */
    if (strnlen(id, BURNSCOPE_CLIENT_ID_MAX) >= BURNSCOPE_CLIENT_ID_MAX) {
        return ESP_ERR_INVALID_ARG;
    }

    /* Skip-write-when-equal — same anti-wear defence as save_creds. */
    char cur[BURNSCOPE_CLIENT_ID_MAX];
    if (nvs_store_load_client_id(agent, cur, sizeof(cur)) == ESP_OK &&
        strcmp(cur, id) == 0) {
        return ESP_OK;
    }

    nvs_handle_t h;
    esp_err_t err = open_rw(NS_PAIR, &h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open RW (pair) failed: %s", esp_err_to_name(err));
        return err;
    }

    err = nvs_set_str(h, agent, id);
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "bound %s -> %s", agent, id);
    } else {
        ESP_LOGE(TAG, "failed to bind %s: %s", agent, esp_err_to_name(err));
    }
    return err;
}

esp_err_t nvs_store_erase_client_ids(void)
{
    nvs_handle_t h;
    esp_err_t err = open_rw(NS_PAIR, &h);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        /* Namespace never existed — nothing to erase. */
        return ESP_OK;
    }
    if (err != ESP_OK) {
        return err;
    }
    /* Wipe everything in the pairing namespace. Safe because this
     * namespace is dedicated to pairing slots — no foreign keys. */
    err = nvs_erase_all(h);
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "erased all pairing slots");
    }
    return err;
}
