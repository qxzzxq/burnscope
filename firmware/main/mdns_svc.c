#include "mdns_svc.h"

#include <stdio.h>
#include <string.h>

#include "esp_err.h"
#include "esp_log.h"
#include "mdns.h"
#include "nvs.h"

#include "device_id.h"
#include "nvs_store.h"
#include "version.h"

static const char *TAG = "mdns";

static bool s_started = false;

/* Per-agent cached TXT value, held across NVS read errors so a transient
 * NVS glitch doesn't flip the advertisement from "1" to "0"
 * permanently. Initialised to "0" (free) at boot. */
static const char *s_paired_cache[] = { "0", "0" };

static size_t _agent_cache_idx(const char *agent)
{
    if (strcmp(agent, "claude") == 0) return 0;
    return 1;
}

/* Returns "1" when the NVS slot is populated, "0" when it is empty
 * (ESP_ERR_NVS_NOT_FOUND). On any other NVS error, returns the previous
 * cached value and logs a warning — matching the HTTP-side fail-closed
 * behaviour so a transient NVS error does not permanently advertise the
 * slot as free. */
static const char *paired_value(const char *agent)
{
    char buf[BURNSCOPE_CLIENT_ID_MAX];
    esp_err_t err = nvs_store_load_client_id(agent, buf, sizeof(buf));
    size_t idx = _agent_cache_idx(agent);

    if (err == ESP_OK) {
        s_paired_cache[idx] = "1";
        return "1";
    }
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        s_paired_cache[idx] = "0";
        return "0";
    }
    ESP_LOGW(TAG, "NVS read error for paired_%s: %s; keeping previous value (%s)",
             agent, esp_err_to_name(err), s_paired_cache[idx]);
    return s_paired_cache[idx];
}

void mdns_svc_start(void)
{
    if (s_started) {
        return;
    }

    ESP_ERROR_CHECK(mdns_init());

    /* Hostname is the device_id — single source of truth (device_id.c).
     * The /health handler emits the same string so the daemon can
     * verify HTTP identity against the cached mDNS hostname. */
    const char *hostname = device_id_str();
    ESP_ERROR_CHECK(mdns_hostname_set(hostname));

    /* Instance name carries the MAC suffix so two devices on one LAN
     * advertise as e.g. "BurnScope f64c" / "BurnScope 5730" instead of
     * relying on Bonjour to auto-rename one to "BurnScope-2". The
     * client's `device_id` keys off the hostname (which is unambiguous
     * either way), but a human-readable, collision-free instance name
     * makes `dns-sd -B` and other tooling much easier to read.
     *
     * Reads the same hostname suffix back so the two strings can't
     * drift (e.g. if device_id ever changed format). */
    char instance[40];
    const char *suffix = hostname + (sizeof("burnscope-") - 1);
    snprintf(instance, sizeof(instance), "BurnScope %s", suffix);
    ESP_ERROR_CHECK(mdns_instance_name_set(instance));

    mdns_txt_item_t txt[] = {
        { "version",       BURNSCOPE_FW_VERSION },
        { "paired_claude", paired_value("claude") },
        { "paired_codex",  paired_value("codex")  },
    };
    ESP_ERROR_CHECK(mdns_service_add(NULL, "_burnscope", "_tcp", 80,
                                     txt, sizeof(txt) / sizeof(txt[0])));

    ESP_LOGI(TAG, "advertising %s._burnscope._tcp.local on port 80 (v%s, claude=%s codex=%s)",
             hostname, BURNSCOPE_FW_VERSION,
             txt[1].value, txt[2].value);

    s_started = true;
}

void mdns_svc_refresh_paired(const char *agent)
{
    if (!s_started || agent == NULL) {
        return;
    }

    char key[24];
    if (strcmp(agent, "claude") == 0) {
        strcpy(key, "paired_claude");
    } else if (strcmp(agent, "codex") == 0) {
        strcpy(key, "paired_codex");
    } else {
        return;
    }

    const char *value = paired_value(agent);
    esp_err_t err = mdns_service_txt_item_set("_burnscope", "_tcp", key, value);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "failed to refresh %s=%s: %s", key, value, esp_err_to_name(err));
        return;
    }
    ESP_LOGI(TAG, "%s=%s", key, value);
}
