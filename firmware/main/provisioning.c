/*
 * Captive-portal provisioning. Open SoftAP `BURNSCOPE-<XXXX>` + DNS hijack
 * + HTTP form. The form does a fetch('/scan.json') after load to populate
 * the SSID list, then POSTs back to /provision; that handler persists
 * creds to NVS and reboots into STA mode.
 *
 * Routes:
 *   GET  /                       → portal HTML
 *   GET  /generate_204           → 200 + portal HTML  (Android probe)
 *   GET  /hotspot-detect.html    → 200 + portal HTML  (iOS probe)
 *   GET  /library/test/success.html → 200 + portal HTML (older iOS probe)
 *   GET  /scan.json              → JSON array of nearby SSIDs + RSSI
 *   POST /provision              → form-encoded ssid+password → save+reboot
 *   *                            → 302 to http://192.168.4.1/
 */

#include "provisioning.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_err.h"
#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"

#include "captive_dns.h"
#include "display_profile.h"
#include "nvs_store.h"
#include "wifi.h"

static const char *TAG = "portal";

static httpd_handle_t s_server = NULL;

/* Embedded form — single screen, vanilla JS for the SSID scan. ~1.5 KiB. */
static const char PORTAL_HTML[] =
"<!DOCTYPE html><html><head>"
"<meta charset='utf-8'>"
"<meta name='viewport' content='width=device-width,initial-scale=1'>"
"<title>BurnScope setup</title>"
"<style>"
"body{font-family:-apple-system,Helvetica,sans-serif;background:#111;color:#eee;"
"margin:0;padding:24px;}"
"h1{font-weight:500;margin:0 0 16px;}"
"label{display:block;margin:12px 0 4px;font-size:14px;color:#aaa;}"
"input,select{width:100%;box-sizing:border-box;padding:10px;border-radius:6px;"
"border:1px solid #333;background:#1c1c1c;color:#eee;font-size:16px;}"
"button{margin-top:20px;width:100%;padding:12px;border:0;border-radius:6px;"
"background:#FF9A3C;color:#111;font-size:16px;font-weight:600;}"
"small{color:#888;}"
"</style></head><body>"
"<h1>Connect BurnScope to WiFi</h1>"
"<form method='POST' action='/provision'>"
"<label for='ssid'>Network</label>"
"<select id='ssid' name='ssid'><option>Scanning&hellip;</option></select>"
"<label for='password'>Password</label>"
"<input id='password' name='password' type='password' autocomplete='off'>"
"<button type='submit'>Save &amp; reboot</button>"
"<p><small>BurnScope will restart into station mode and join the network.</small></p>"
"</form>"
"<script>"
"fetch('/scan.json').then(r=>r.json()).then(list=>{"
"const sel=document.getElementById('ssid');sel.innerHTML='';"
"list.forEach(n=>{const o=document.createElement('option');"
"o.value=n.ssid;o.text=n.ssid+' ('+n.rssi+' dBm)';sel.appendChild(o);});"
"if(!list.length){const o=document.createElement('option');"
"o.text='(no networks found — retype manually)';sel.appendChild(o);}"
"}).catch(()=>{});"
"</script></body></html>";

static esp_err_t send_portal(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html");
    httpd_resp_set_status(req, "200 OK");
    return httpd_resp_send(req, PORTAL_HTML, sizeof(PORTAL_HTML) - 1);
}

static esp_err_t root_get(httpd_req_t *req)        { return send_portal(req); }
static esp_err_t probe_android(httpd_req_t *req)   { return send_portal(req); }
static esp_err_t probe_apple(httpd_req_t *req)     { return send_portal(req); }
static esp_err_t probe_apple_old(httpd_req_t *req) { return send_portal(req); }

static esp_err_t catchall_get(httpd_req_t *req)
{
    httpd_resp_set_status(req, "302 Found");
    httpd_resp_set_hdr(req, "Location", "http://192.168.4.1/");
    return httpd_resp_send(req, NULL, 0);
}

static esp_err_t scan_get(httpd_req_t *req)
{
    /* Trigger a synchronous-ish scan. APSTA mode is already active. */
    wifi_scan_config_t cfg = { .scan_type = WIFI_SCAN_TYPE_ACTIVE };
    esp_wifi_scan_start(&cfg, true);

    uint16_t n = 0;
    esp_wifi_scan_get_ap_num(&n);
    if (n > 16) n = 16;
    wifi_ap_record_t *aps = NULL;
    if (n > 0) {
        aps = calloc(n, sizeof(*aps));
        if (aps == NULL) {
            return httpd_resp_send_500(req);
        }
        esp_wifi_scan_get_ap_records(&n, aps);
    }

    /* Build JSON. Each entry is at most ~80 bytes; cap at 1.5 KiB. */
    char body[1536];
    int off = 0;
    off += snprintf(body + off, sizeof(body) - off, "[");
    for (int i = 0; i < n; ++i) {
        if (aps[i].ssid[0] == 0) continue;
        if (off > (int)sizeof(body) - 100) break;
        off += snprintf(body + off, sizeof(body) - off,
                        "%s{\"ssid\":\"%s\",\"rssi\":%d}",
                        (i == 0) ? "" : ",",
                        (char *)aps[i].ssid,
                        (int)aps[i].rssi);
    }
    off += snprintf(body + off, sizeof(body) - off, "]");
    free(aps);

    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, body, off);
}

/* Tiny x-www-form-urlencoded value extractor. Decodes %XX and `+`. */
static bool extract_field(const char *body, const char *key, char *out, size_t out_len)
{
    size_t key_len = strlen(key);
    const char *p = body;
    while (*p) {
        if (strncmp(p, key, key_len) == 0 && p[key_len] == '=') {
            p += key_len + 1;
            size_t i = 0;
            while (*p && *p != '&' && i + 1 < out_len) {
                if (*p == '+') {
                    out[i++] = ' ';
                    p++;
                } else if (*p == '%' && p[1] && p[2]) {
                    char hex[3] = { p[1], p[2], 0 };
                    out[i++] = (char)strtol(hex, NULL, 16);
                    p += 3;
                } else {
                    out[i++] = *p++;
                }
            }
            out[i] = '\0';
            return true;
        }
        while (*p && *p != '&') p++;
        if (*p == '&') p++;
    }
    return false;
}

static void delayed_restart_cb(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "rebooting into STA mode");
    esp_restart();
}

static esp_err_t provision_post(httpd_req_t *req)
{
    if (req->content_len > 512) {
        return httpd_resp_send_err(req, HTTPD_413_CONTENT_TOO_LARGE, "form too large");
    }
    char buf[513];
    int got = httpd_req_recv(req, buf, sizeof(buf) - 1);
    if (got <= 0) {
        return ESP_FAIL;
    }
    buf[got] = '\0';

    wifi_creds_t c = { 0 };
    if (!extract_field(buf, "ssid", c.ssid, sizeof(c.ssid)) ||
        c.ssid[0] == '\0') {
        return httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "missing ssid");
    }
    extract_field(buf, "password", c.password, sizeof(c.password));

    esp_err_t err = nvs_store_save_creds(&c);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "save failed: %s", esp_err_to_name(err));
        return httpd_resp_send_500(req);
    }

    const char *done = "<html><body style='font-family:sans-serif;background:#111;color:#eee;"
                       "padding:24px'><h1>Saved.</h1><p>Rebooting&hellip;</p></body></html>";
    httpd_resp_set_type(req, "text/html");
    httpd_resp_send(req, done, strlen(done));

    /* Reboot ~500 ms later so the 200 actually flushes before the radio dies. */
    const esp_timer_create_args_t targs = {
        .callback = delayed_restart_cb,
        .name = "portal_reboot",
    };
    esp_timer_handle_t t;
    if (esp_timer_create(&targs, &t) == ESP_OK) {
        esp_timer_start_once(t, 500 * 1000);
    } else {
        esp_restart();
    }
    return ESP_OK;
}

static void register_routes(void)
{
    const httpd_uri_t routes[] = {
        { .uri = "/",                         .method = HTTP_GET,  .handler = root_get },
        { .uri = "/generate_204",             .method = HTTP_GET,  .handler = probe_android },
        { .uri = "/gen_204",                  .method = HTTP_GET,  .handler = probe_android },
        { .uri = "/hotspot-detect.html",      .method = HTTP_GET,  .handler = probe_apple },
        { .uri = "/library/test/success.html",.method = HTTP_GET,  .handler = probe_apple_old },
        { .uri = "/scan.json",                .method = HTTP_GET,  .handler = scan_get },
        { .uri = "/provision",                .method = HTTP_POST, .handler = provision_post },
    };
    for (size_t i = 0; i < sizeof(routes) / sizeof(routes[0]); ++i) {
        ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &routes[i]));
    }
    /* Catch-all for any other GET — must register LAST so explicit routes
     * win. esp_http_server's wildcard match is greedy. */
    const httpd_uri_t catchall = {
        .uri = "/*", .method = HTTP_GET, .handler = catchall_get,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &catchall));
}

static void compute_ap_ssid(char *out, size_t n)
{
    uint8_t mac[6] = { 0 };
    esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
    snprintf(out, n, "BURNSCOPE-%02X%02X", mac[4], mac[5]);
}

void provisioning_start(void)
{
    char ssid[24];
    compute_ap_ssid(ssid, sizeof(ssid));

    /* Paint the splash with the real SSID and the portal URL — Apple/
     * Android usually auto-open the portal from their captive-network
     * probe, but the URL gives the user a fallback when the OS doesn't. */
    char splash[96];
    snprintf(splash, sizeof(splash),
             "Setup mode\nJoin %s\nOpen 192.168.4.1", ssid);
    display_profile_show_status(splash);

    wifi_start_ap(ssid);

    captive_dns_start();

    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = 80;
    cfg.lru_purge_enable = true;
    cfg.uri_match_fn = httpd_uri_match_wildcard;
    cfg.max_uri_handlers = 12;

    ESP_ERROR_CHECK(httpd_start(&s_server, &cfg));
    register_routes();

    ESP_LOGI(TAG, "captive portal up; AP '%s' on http://192.168.4.1/", ssid);
}
