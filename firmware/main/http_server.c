/*
 * Production HTTP server. Parses POST /summary into the snapshot store
 * and exposes GET /health for the daemon to age-check. The previously-
 * registered POST /factory-reset has been pulled until an auth scheme
 * exists; the BOOT-button long-press in factory_reset.c remains as the
 * physical-presence reset path in the meantime.
 *
 * JSON parsing is hand-rolled against the very tight schema in
 * `docs/wire-format.md`. cJSON is not in ESP-IDF v6 and pulling it in as
 * a managed dependency for one ~200-byte payload didn't earn its keep.
 */

#include "http_server.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_err.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "nvs.h"

#include "mdns_svc.h"
#include "nvs_store.h"
#include "snapshot.h"
#include "version.h"

static const char *TAG = "http";

#define SUMMARY_MAX_BODY  (16 * 1024)

static httpd_handle_t s_server = NULL;

/* --------------------------------------------------------------------- */
/* Minimal JSON cursor — parses the AgentSnapshot shape and nothing else.*/
/* --------------------------------------------------------------------- */

typedef struct {
    const char *p;
    const char *end;
    bool        err;
} cursor_t;

static void skip_ws(cursor_t *c)
{
    while (c->p < c->end && isspace((unsigned char)*c->p)) c->p++;
}

static bool eat(cursor_t *c, char ch)
{
    skip_ws(c);
    if (c->p < c->end && *c->p == ch) { c->p++; return true; }
    c->err = true;
    return false;
}

/* Read a string. Stores into out[out_len] with NUL. Supports only
 * printable ASCII + the standard backslash escapes; rejects \uXXXX since
 * the wire schema never uses them in MVP. */
static bool read_string(cursor_t *c, char *out, size_t out_len)
{
    skip_ws(c);
    if (c->p >= c->end || *c->p != '"') { c->err = true; return false; }
    c->p++;
    size_t i = 0;
    while (c->p < c->end && *c->p != '"') {
        char ch = *c->p++;
        if (ch == '\\') {
            if (c->p >= c->end) { c->err = true; return false; }
            char esc = *c->p++;
            switch (esc) {
            case '"': case '\\': case '/': ch = esc; break;
            case 'n': ch = '\n'; break;
            case 't': ch = '\t'; break;
            case 'r': ch = '\r'; break;
            default: c->err = true; return false;
            }
        }
        if (i + 1 < out_len) out[i++] = ch;
    }
    if (c->p >= c->end) { c->err = true; return false; }
    c->p++;
    if (out_len > 0) out[i] = '\0';
    return true;
}

/* Read a numeric value as a double (covers integers + 0.0..1.0 floats). */
static bool read_number(cursor_t *c, double *out)
{
    skip_ws(c);
    char *endp = NULL;
    /* strtod operates on a NUL-terminated buffer; the body is already
     * NUL-terminated by the caller. */
    double v = strtod(c->p, &endp);
    if (endp == c->p) { c->err = true; return false; }
    c->p = endp;
    *out = v;
    return true;
}

/* Read a key at the current position into out (no value). Used at every
 * object key — the wire format isn't strict about ordering. */
static bool peek_key(cursor_t *c, char *out, size_t out_len)
{
    skip_ws(c);
    if (c->p >= c->end || *c->p != '"') return false;
    const char *save = c->p;
    if (!read_string(c, out, out_len)) {
        c->p = save; return false;
    }
    skip_ws(c);
    if (c->p >= c->end || *c->p != ':') { c->err = true; return false; }
    c->p++;
    return true;
}

/* --------------------------------------------------------------------- */
/* POST /summary                                                          */
/* --------------------------------------------------------------------- */

static esp_err_t reject_400(httpd_req_t *req, const char *reason)
{
    ESP_LOGW(TAG, "rejecting /summary: %s", reason);
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, reason);
    return ESP_OK;
}

static const char *const KNOWN_AGENTS[] = { "claude", "codex" };
#define KNOWN_AGENT_COUNT (sizeof(KNOWN_AGENTS) / sizeof(KNOWN_AGENTS[0]))

static bool is_known_agent(const char *s)
{
    if (s == NULL) return false;
    for (size_t i = 0; i < KNOWN_AGENT_COUNT; ++i) {
        if (strcmp(s, KNOWN_AGENTS[i]) == 0) return true;
    }
    return false;
}

/*
 * Read `X-BurnScope-Client-Id` into `out`. Returns:
 *   - 0 on success. `out` is NUL-terminated; an empty string means the
 *     header was absent. Callers treat absent and present-with-value
 *     differently (e.g. /health allows absent when no slot is bound).
 *   - -1 if the header was present but unusable: too long for `cap`,
 *     unreadable from the request, or containing bytes outside the
 *     printable-ASCII set documented in `docs/wire-format.md` (control
 *     bytes < 0x20, or non-ASCII ≥ 0x7F). `"` and `\\` are accepted —
 *     /health JSON-escapes them on output. Caller should 401.
 * `out` is always NUL-terminated on return.
 */
static bool client_id_byte_ok(unsigned char c)
{
    /* Printable ASCII, matching `docs/wire-format.md`. `"` and `\\` are
     * allowed — `/health` JSON-escapes them on output. Control bytes and
     * non-ASCII (≥ 0x7F) stay rejected to keep stored identifiers
     * single-line, byte-counted, and renderable by the device. */
    return c >= 0x20 && c < 0x7F;
}

static int read_client_id_header(httpd_req_t *req, char *out, size_t cap)
{
    out[0] = '\0';
    size_t len = httpd_req_get_hdr_value_len(req, "X-BurnScope-Client-Id");
    if (len == 0) {
        return 0;
    }
    if (len >= cap) {
        return -1;
    }
    /* httpd_req_get_hdr_value_str needs cap >= len + 1; we already
     * verified that above. */
    if (httpd_req_get_hdr_value_str(req, "X-BurnScope-Client-Id", out, cap) != ESP_OK) {
        out[0] = '\0';
        return -1;
    }
    for (size_t i = 0; out[i] != '\0'; ++i) {
        if (!client_id_byte_ok((unsigned char)out[i])) {
            out[0] = '\0';
            return -1;
        }
    }
    return 0;
}

/*
 * TOFU pairing check for `agent`. Returns:
 *   - true  → accept and continue (slot was empty and we just saved the
 *             header, or the header matched the stored value).
 *   - false → 401 already sent on `req`; caller returns ESP_OK.
 *
 * Empty header is treated as a hard reject — v2 collectors always send
 * one, so a missing header on /summary means a misconfigured client. */
static bool authorize_summary(httpd_req_t *req, const char *agent)
{
    char header[BURNSCOPE_CLIENT_ID_MAX];
    if (read_client_id_header(req, header, sizeof(header)) != 0 || header[0] == '\0') {
        ESP_LOGW(TAG, "/summary missing or oversized X-BurnScope-Client-Id");
        httpd_resp_set_status(req, "401 Unauthorized");
        httpd_resp_set_type(req, "application/json");
        const char *body = "{\"error\":\"client id required\"}";
        httpd_resp_send(req, body, strlen(body));
        return false;
    }

    char stored[BURNSCOPE_CLIENT_ID_MAX];
    esp_err_t err = nvs_store_load_client_id(agent, stored, sizeof(stored));
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        /* TOFU: first push for this agent claims the slot. */
        esp_err_t s = nvs_store_save_client_id(agent, header);
        if (s != ESP_OK) {
            ESP_LOGE(TAG, "failed to bind %s on first push: %s",
                     agent, esp_err_to_name(s));
            httpd_resp_send_500(req);
            return false;
        }
        /* Flip paired_<agent>=1 in the mDNS TXT so other clients on the
         * LAN stop offering this slot for pairing. */
        mdns_svc_refresh_paired(agent);
        /* Don't log the bound identifier (typically an email) at info
         * level — keep PII out of the serial log. Length is enough to
         * confirm a non-empty bind for diagnostics. */
        ESP_LOGI(TAG, "TOFU bound %s (id %u bytes)", agent, (unsigned)strlen(header));
        return true;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_store_load_client_id(%s) failed: %s",
                 agent, esp_err_to_name(err));
        httpd_resp_send_500(req);
        return false;
    }

    if (strcmp(stored, header) != 0) {
        ESP_LOGW(TAG, "/summary client-id mismatch for %s", agent);
        httpd_resp_set_status(req, "401 Unauthorized");
        httpd_resp_set_type(req, "application/json");
        const char *body = "{\"error\":\"client id mismatch\"}";
        httpd_resp_send(req, body, strlen(body));
        return false;
    }
    return true;
}

/* Parse one {"type":..,"used_pct":..,"resets_at":..} object. Accepts the
 * three keys in any order. */
static bool parse_session(cursor_t *c, session_snapshot_t *out)
{
    if (!eat(c, '{')) return false;
    bool have_type = false, have_pct = false, have_reset = false;
    memset(out, 0, sizeof(*out));

    for (;;) {
        skip_ws(c);
        if (c->p < c->end && *c->p == '}') { c->p++; break; }

        char key[16];
        if (!peek_key(c, key, sizeof(key))) return false;

        if (strcmp(key, "type") == 0) {
            if (!read_string(c, out->type, sizeof(out->type))) return false;
            have_type = true;
        } else if (strcmp(key, "used_pct") == 0) {
            double v;
            if (!read_number(c, &v)) return false;
            if (v < 0.0 || v > 1.0) { c->err = true; return false; }
            out->used_pct = (float)v;
            have_pct = true;
        } else if (strcmp(key, "resets_at") == 0) {
            double v;
            if (!read_number(c, &v)) return false;
            out->resets_at = (int64_t)v;
            have_reset = true;
        } else {
            /* Unknown key — bail; wire format is strict. */
            c->err = true;
            return false;
        }

        skip_ws(c);
        if (c->p < c->end && *c->p == ',') { c->p++; continue; }
        if (c->p < c->end && *c->p == '}') { c->p++; break; }
        c->err = true;
        return false;
    }

    if (!have_type || !have_pct || !have_reset || out->type[0] == '\0') {
        return false;
    }
    return true;
}

static bool parse_snapshot(cursor_t *c, agent_snapshot_t *out)
{
    if (!eat(c, '{')) return false;
    memset(out, 0, sizeof(*out));
    bool have_agent = false, have_captured = false, have_sessions = false;

    for (;;) {
        skip_ws(c);
        if (c->p < c->end && *c->p == '}') { c->p++; break; }

        char key[16];
        if (!peek_key(c, key, sizeof(key))) return false;

        if (strcmp(key, "agent") == 0) {
            if (!read_string(c, out->agent, sizeof(out->agent))) return false;
            if (!is_known_agent(out->agent)) { c->err = true; return false; }
            have_agent = true;
        } else if (strcmp(key, "captured_at") == 0) {
            double v;
            if (!read_number(c, &v)) return false;
            out->captured_at = (int64_t)v;
            have_captured = true;
        } else if (strcmp(key, "sessions") == 0) {
            if (!eat(c, '[')) return false;
            out->session_count = 0;
            for (;;) {
                skip_ws(c);
                if (c->p < c->end && *c->p == ']') { c->p++; break; }
                if (out->session_count >= SNAPSHOT_MAX_SESSIONS) {
                    /* Drop extras silently — schema allows future tiers. */
                    /* Skip remaining entries by counting braces. */
                    int depth = 0;
                    while (c->p < c->end) {
                        char ch = *c->p++;
                        if (ch == '{') depth++;
                        else if (ch == '}') {
                            if (depth == 0) { c->err = true; return false; }
                            depth--;
                        } else if (ch == ']' && depth == 0) {
                            goto sessions_done;
                        }
                    }
                    c->err = true;
                    return false;
                }
                if (!parse_session(c, &out->sessions[out->session_count])) {
                    return false;
                }
                out->session_count++;
                skip_ws(c);
                if (c->p < c->end && *c->p == ',') { c->p++; continue; }
                if (c->p < c->end && *c->p == ']') { c->p++; break; }
                c->err = true;
                return false;
            }
sessions_done:
            have_sessions = true;
        } else {
            c->err = true;
            return false;
        }

        skip_ws(c);
        if (c->p < c->end && *c->p == ',') { c->p++; continue; }
        if (c->p < c->end && *c->p == '}') { c->p++; break; }
        c->err = true;
        return false;
    }

    return have_agent && have_captured && have_sessions && out->session_count > 0;
}

static esp_err_t summary_post_handler(httpd_req_t *req)
{
    int content_len = req->content_len;
    if (content_len < 0) content_len = 0;
    if (content_len > SUMMARY_MAX_BODY) {
        ESP_LOGW(TAG, "body too large (%d bytes); dropping", content_len);
        httpd_resp_send_err(req, HTTPD_413_CONTENT_TOO_LARGE, "body too large");
        return ESP_OK;
    }
    if (content_len == 0) {
        return reject_400(req, "empty body");
    }

    char *buf = malloc(content_len + 1);
    if (buf == NULL) {
        return httpd_resp_send_500(req);
    }
    int got_total = 0;
    while (got_total < content_len) {
        int got = httpd_req_recv(req, buf + got_total, content_len - got_total);
        if (got <= 0) {
            if (got == HTTPD_SOCK_ERR_TIMEOUT) continue;
            free(buf);
            return ESP_FAIL;
        }
        got_total += got;
    }
    buf[got_total] = '\0';

    agent_snapshot_t snap;
    cursor_t cur = { .p = buf, .end = buf + got_total, .err = false };
    bool ok = parse_snapshot(&cur, &snap);
    free(buf);

    if (!ok || cur.err) {
        return reject_400(req, "invalid AgentSnapshot");
    }

    /* Pairing check runs after parse so we know the agent name; runs
     * before snapshot_store_put so a 401 leaves no state behind. */
    if (!authorize_summary(req, snap.agent)) {
        return ESP_OK;
    }

    switch (snapshot_store_put(&snap)) {
    case SNAPSHOT_PUT_OK:
        ESP_LOGI(TAG, "snapshot accepted: agent=%s sessions=%d",
                 snap.agent, snap.session_count);
        httpd_resp_set_status(req, "204 No Content");
        httpd_resp_send(req, NULL, 0);
        return ESP_OK;
    case SNAPSHOT_PUT_STALE: {
        /* Monotonic-guard reject. Honest about *why* with a 409 so the
         * daemon can log/skip rather than silently retry. */
        const char *body = "{\"error\":\"stale captured_at\"}";
        httpd_resp_set_status(req, "409 Conflict");
        httpd_resp_set_type(req, "application/json");
        httpd_resp_send(req, body, strlen(body));
        return ESP_OK;
    }
    case SNAPSHOT_PUT_NO_SLOT:
    default:
        return httpd_resp_send_500(req);
    }
}

/* --------------------------------------------------------------------- */
/* GET /health                                                            */
/* --------------------------------------------------------------------- */

typedef struct {
    char  *body;
    size_t cap;
    size_t off;
    bool   first;
} agents_writer_t;

/* Stream-write `in` into `w->body[off..cap]` with the JSON escapes
 * required between surrounding `"..."` quotes. Bytes the wire-format
 * input filter doesn't catch (`"` and `\\` survive intake; future agent
 * `type` values are entirely agent-defined) get rewritten as `\"` /
 * `\\`; newline/tab/CR map to their short forms. Returns false on
 * buffer overflow — caller's outer rollback restores w->off. */
static bool json_escape_append(agents_writer_t *w, const char *in)
{
    static const char HEX[] = "0123456789abcdef";
    for (const unsigned char *p = (const unsigned char *)in; *p != '\0'; ++p) {
        char tiny[7];
        const char *seq;
        size_t len;
        switch (*p) {
        case '"':  seq = "\\\""; len = 2; break;
        case '\\': seq = "\\\\"; len = 2; break;
        case '\n': seq = "\\n";  len = 2; break;
        case '\r': seq = "\\r";  len = 2; break;
        case '\t': seq = "\\t";  len = 2; break;
        default:
            if (*p < 0x20) {
                tiny[0]='\\'; tiny[1]='u'; tiny[2]='0'; tiny[3]='0';
                tiny[4]=HEX[*p >> 4]; tiny[5]=HEX[*p & 0xF]; tiny[6]='\0';
                seq = tiny; len = 6;
            } else {
                tiny[0] = (char)*p; tiny[1] = '\0';
                seq = tiny; len = 1;
            }
            break;
        }
        if (w->off + len > w->cap) return false;
        memcpy(w->body + w->off, seq, len);
        w->off += len;
    }
    return true;
}

/* Append one agent entry to the /health JSON. Each entry carries the
 * bound `client_id` (so daemons can detect drift after a factory
 * reset), `seconds_since_last_push` (diagnostics), and `sessions` (the
 * same shape the daemon POSTs — used to spot when firmware-side state
 * diverges from the latest upstream probe after an ESP32 reboot).
 *
 * The variable-content strings (`agent`, `client_id`, `type`) are JSON-
 * escaped on output so values containing `"` / `\\` / control bytes
 * can't break the response.
 *
 * Worst case under SNAPSHOT_MAX_SESSIONS=3:
 *   "claude":{"client_id":"<254ch>","seconds_since_last_push":<int64>,
 *             "sessions":[{"type":"<16ch>","used_pct":<%g>,"resets_at":<int64>}, ... x3]}
 * fits in ~520 bytes (escaping doubles each metacharacter; we don't
 * size for full 6x worst case because intake already rejects control
 * bytes in client_id and agent values are constants).
 */
static void append_agent(const agent_snapshot_t *snap, void *user)
{
    agents_writer_t *w = (agents_writer_t *)user;
    if (w->off >= w->cap) return;

    int64_t age = snapshot_store_age_s(snap->agent);
    size_t saved_off = w->off;
    bool saved_first = w->first;

    /* Look up the bound client id once per entry. Empty if the slot was
     * cleared (e.g. after a factory reset that wiped pairing but left
     * the in-RAM snapshot — currently can't happen, but defensive). */
    char cid[BURNSCOPE_CLIENT_ID_MAX] = "";
    (void)nvs_store_load_client_id(snap->agent, cid, sizeof(cid));

    int n;
    if (!w->first) {
        if (w->off + 1 > w->cap) goto overflow;
        w->body[w->off++] = ',';
    }
    if (w->off + 1 > w->cap) goto overflow;
    w->body[w->off++] = '"';
    if (!json_escape_append(w, snap->agent)) goto overflow;
    n = snprintf(w->body + w->off, w->cap - w->off, "\":{\"client_id\":\"");
    if (n <= 0 || (size_t)n >= w->cap - w->off) goto overflow;
    w->off += n;
    if (!json_escape_append(w, cid)) goto overflow;
    n = snprintf(w->body + w->off, w->cap - w->off,
                 "\",\"seconds_since_last_push\":%lld,\"sessions\":[",
                 (long long)age);
    if (n <= 0 || (size_t)n >= w->cap - w->off) goto overflow;
    w->off += n;

    for (uint8_t i = 0; i < snap->session_count; ++i) {
        const session_snapshot_t *s = &snap->sessions[i];
        n = snprintf(w->body + w->off, w->cap - w->off,
                     "%s{\"type\":\"", i == 0 ? "" : ",");
        if (n <= 0 || (size_t)n >= w->cap - w->off) goto overflow;
        w->off += n;
        if (!json_escape_append(w, s->type)) goto overflow;
        n = snprintf(w->body + w->off, w->cap - w->off,
                     "\",\"used_pct\":%.6g,\"resets_at\":%lld}",
                     (double)s->used_pct,
                     (long long)s->resets_at);
        if (n <= 0 || (size_t)n >= w->cap - w->off) goto overflow;
        w->off += n;
    }

    n = snprintf(w->body + w->off, w->cap - w->off, "]}");
    if (n <= 0 || (size_t)n >= w->cap - w->off) goto overflow;
    w->off += n;

    w->first = false;
    return;

overflow:
    /* Roll back any partial write so the outer JSON stays valid. */
    w->off = saved_off;
    w->first = saved_first;
    if (w->off < w->cap) {
        w->body[w->off] = '\0';
    }
}

/*
 * /health authorization: the daemon's probe must match *any* populated
 * pairing slot. With no slots populated yet (fresh device), allow the
 * header-less or any-value probe so the daemon's first cycle can
 * discover the device before its first POST claims a slot. Returns
 * true → continue; false → 401 already sent.
 */
static bool authorize_health(httpd_req_t *req)
{
    /* Survey populated slots first. */
    char slots[KNOWN_AGENT_COUNT][BURNSCOPE_CLIENT_ID_MAX];
    bool slot_filled[KNOWN_AGENT_COUNT] = { false };
    int  filled_count = 0;
    for (size_t i = 0; i < KNOWN_AGENT_COUNT; ++i) {
        slots[i][0] = '\0';
        esp_err_t err = nvs_store_load_client_id(KNOWN_AGENTS[i], slots[i], sizeof(slots[i]));
        if (err == ESP_OK) {
            slot_filled[i] = true;
            filled_count++;
        } else if (err == ESP_ERR_NVS_NOT_FOUND) {
            /* Empty slot — leave slot_filled[i] = false. */
        } else {
            /* Real NVS failure: fail closed. Treating it as "empty" would
             * let an unauthenticated probe through (filled_count stays 0
             * → return true on the next branch) even though a binding
             * may actually exist but be unreadable. */
            ESP_LOGE(TAG, "/health: NVS read failed for %s: %s — failing closed",
                     KNOWN_AGENTS[i], esp_err_to_name(err));
            httpd_resp_send_500(req);
            return false;
        }
    }
    if (filled_count == 0) {
        /* No bindings yet — accept anything so first contact works. */
        return true;
    }

    char header[BURNSCOPE_CLIENT_ID_MAX];
    if (read_client_id_header(req, header, sizeof(header)) != 0 || header[0] == '\0') {
        ESP_LOGW(TAG, "/health missing X-BurnScope-Client-Id (have %d binding(s))",
                 filled_count);
        httpd_resp_set_status(req, "401 Unauthorized");
        httpd_resp_set_type(req, "application/json");
        const char *body = "{\"error\":\"client id required\"}";
        httpd_resp_send(req, body, strlen(body));
        return false;
    }

    for (size_t i = 0; i < KNOWN_AGENT_COUNT; ++i) {
        if (slot_filled[i] && strcmp(slots[i], header) == 0) {
            return true;
        }
    }

    ESP_LOGW(TAG, "/health client-id matches no populated slot");
    httpd_resp_set_status(req, "401 Unauthorized");
    httpd_resp_set_type(req, "application/json");
    const char *body = "{\"error\":\"client id mismatch\"}";
    httpd_resp_send(req, body, strlen(body));
    return false;
}

static esp_err_t health_get_handler(httpd_req_t *req)
{
    if (!authorize_health(req)) {
        return ESP_OK;
    }

    int64_t uptime_us = esp_timer_get_time();
    uint32_t uptime_s = (uint32_t)(uptime_us / 1000000);
    uint32_t free_heap = esp_get_free_heap_size();

    /* 2 KiB holds the envelope plus two enriched agent entries (each
     * including the bound client_id, capped at 255 bytes) with headroom.
     *
     * Heap-allocated — keeping this on the httpd task stack overflowed
     * the 4 KiB default once `append_agent` nested a 255-byte `cid` plus
     * snprintf scratch on top: any /health probe under load would panic
     * the httpd task. The heap path costs one malloc/free per /health
     * (called ~every 30 s by the daemon), trivially cheap. */
    const size_t cap = 2048;
    char *body = malloc(cap);
    if (body == NULL) {
        return httpd_resp_send_500(req);
    }

    int off = snprintf(body, cap,
                       "{\"firmware_version\":\"%s\","
                       "\"uptime_s\":%lu,"
                       "\"free_heap_b\":%lu,"
                       "\"agents\":{",
                       BURNSCOPE_FW_VERSION,
                       (unsigned long)uptime_s,
                       (unsigned long)free_heap);
    /* snprintf returns <0 on encoding error and >=cap on truncation.
     * Either would corrupt agents_writer_t's off / overflow detection;
     * fail cleanly instead. */
    if (off < 0 || (size_t)off >= cap) {
        free(body);
        return httpd_resp_send_500(req);
    }

    agents_writer_t w = { .body = body, .cap = cap, .off = (size_t)off, .first = true };
    snapshot_store_foreach(append_agent, &w);
    /* Need at least 2 bytes for the closing `}}`. If the agents writer
     * filled the buffer right up to the edge, sending what we have
     * would produce truncated JSON; surface that as a 500 instead. */
    if (w.off + 2 > w.cap) {
        free(body);
        return httpd_resp_send_500(req);
    }
    w.body[w.off++] = '}';
    w.body[w.off++] = '}';
    if (w.off < w.cap) {
        w.body[w.off] = '\0';
    }

    httpd_resp_set_type(req, "application/json");
    esp_err_t send_err = httpd_resp_send(req, body, w.off);
    free(body);
    return send_err;
}

/* --------------------------------------------------------------------- */
/* Startup                                                                */
/* --------------------------------------------------------------------- */

/* Note: a POST /factory-reset endpoint used to live here. It was
 * removed because the route had no authentication — anything on the
 * LAN could wipe the device. Re-introduce alongside a proper auth
 * scheme; the BOOT long-press in factory_reset.c remains as the
 * physical-presence reset path until then. */

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
        .uri = "/summary", .method = HTTP_POST, .handler = summary_post_handler,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &summary));

    const httpd_uri_t health = {
        .uri = "/health", .method = HTTP_GET, .handler = health_get_handler,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &health));

    ESP_LOGI(TAG, "HTTP server listening on :80");
}
