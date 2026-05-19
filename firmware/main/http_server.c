#include "http_server.h"

#include <stdio.h>
#include <string.h>

#include "esp_err.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"

#include "version.h"

static const char *TAG = "http";

#define SUMMARY_MAX_BODY (16 * 1024)
#define SUMMARY_CHUNK    512

static httpd_handle_t s_server = NULL;

static esp_err_t summary_post_handler(httpd_req_t *req)
{
    char chunk[SUMMARY_CHUNK];
    int remaining = req->content_len;
    if (remaining < 0) {
        remaining = 0;
    }
    /* Phase 2 will parse the body. Phase 1 only proves the route shape. */
    int total_drained = 0;
    while (remaining > 0) {
        int want = remaining > (int)sizeof(chunk) ? (int)sizeof(chunk) : remaining;
        int got = httpd_req_recv(req, chunk, want);
        if (got <= 0) {
            if (got == HTTPD_SOCK_ERR_TIMEOUT) {
                continue;
            }
            return ESP_FAIL;
        }
        remaining -= got;
        total_drained += got;
        if (total_drained > SUMMARY_MAX_BODY) {
            ESP_LOGW(TAG, "body too large (>%d bytes); dropping", SUMMARY_MAX_BODY);
            httpd_resp_send_err(req, HTTPD_413_CONTENT_TOO_LARGE, "body too large");
            return ESP_OK;
        }
    }

    httpd_resp_set_status(req, "204 No Content");
    httpd_resp_send(req, NULL, 0);
    return ESP_OK;
}

static esp_err_t health_get_handler(httpd_req_t *req)
{
    int64_t uptime_us = esp_timer_get_time();
    uint32_t uptime_s = (uint32_t)(uptime_us / 1000000);
    uint32_t free_heap = esp_get_free_heap_size();

    char body[160];
    int n = snprintf(body, sizeof(body),
                     "{\"firmware_version\":\"%s\",\"uptime_s\":%lu,\"free_heap_b\":%lu}",
                     BURNSCOPE_FW_VERSION,
                     (unsigned long)uptime_s,
                     (unsigned long)free_heap);
    httpd_resp_set_type(req, "application/json");
    httpd_resp_send(req, body, n);
    return ESP_OK;
}

void http_server_start(void)
{
    if (s_server != NULL) {
        return;
    }

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 80;
    config.uri_match_fn = httpd_uri_match_wildcard;

    ESP_ERROR_CHECK(httpd_start(&s_server, &config));

    const httpd_uri_t summary = {
        .uri = "/summary",
        .method = HTTP_POST,
        .handler = summary_post_handler,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &summary));

    const httpd_uri_t health = {
        .uri = "/health",
        .method = HTTP_GET,
        .handler = health_get_handler,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &health));

    ESP_LOGI(TAG, "HTTP server listening on :80");
}
