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
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"

#include "mdns_svc.h"
#include "nvs_store.h"
#include "snapshot.h"
#include "version.h"

static const char *TAG = "http";

#define SUMMARY_MAX_BODY  (16 * 1024)
/* Loose upper bound — slightly above an OTA slot size (5 MB) so a
 * malformed Content-Length can't make us malloc-and-stream forever
 * but a legitimate full-fat image fits comfortably. */
#define OTA_MAX_BODY      (6 * 1024 * 1024)
#define OTA_RECV_CHUNK    4096

static httpd_handle_t   s_server     = NULL;
/* Serializes /ota across concurrent uploads. Lazily initialized in
 * http_server_start; held for the duration of one OTA stream + commit.
 * Two parallel uploads would race esp_ota_write into the same
 * partition and produce a corrupt image. */
static SemaphoreHandle_t s_ota_mutex = NULL;

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
 * the wire schema never uses them in MVP.
 *
 * Fails (sets c->err and returns false) if the JSON value is longer than
 * out_len-1 bytes. Silent truncation here would corrupt fixed-size
 * fields like session_type (16-byte cap) and let the daemon's
 * `/health` divergence detector chase the corruption forever
 * (deep-review M-3). */
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
        if (i + 1 >= out_len) {
            c->err = true;
            return false;
        }
        out[i++] = ch;
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

/* Consume a JSON value of any type without interpreting it. Used to skip
 * over unknown keys so the firmware tolerates a newer daemon adding wire
 * fields it doesn't yet render. Handles strings (with backslash escapes),
 * numbers/booleans/null, and nested objects/arrays via brace/bracket
 * depth counting. */
static bool skip_string_body(cursor_t *c)
{
    /* Caller has already consumed the opening '"'. Consume bytes until
     * the matching closing '"', honouring backslash escapes so a
     * `"\""` doesn't terminate early. */
    while (c->p < c->end && *c->p != '"') {
        if (*c->p == '\\' && c->p + 1 < c->end) c->p += 2;
        else c->p++;
    }
    if (c->p >= c->end) { c->err = true; return false; }
    c->p++;
    return true;
}

static bool skip_value(cursor_t *c)
{
    skip_ws(c);
    if (c->p >= c->end) { c->err = true; return false; }
    char ch = *c->p;
    if (ch == '"') {
        c->p++;
        return skip_string_body(c);
    }
    if (ch == '{' || ch == '[') {
        char open = ch;
        char close = (ch == '{') ? '}' : ']';
        int depth = 1;
        c->p++;
        while (c->p < c->end && depth > 0) {
            char cc = *c->p++;
            if (cc == '"') {
                if (!skip_string_body(c)) return false;
            } else if (cc == open) {
                depth++;
            } else if (cc == close) {
                depth--;
            }
        }
        if (depth != 0) { c->err = true; return false; }
        return true;
    }
    if (ch == 't' || ch == 'f' || ch == 'n') {
        const char *lit;
        size_t lit_len;
        if (ch == 't') { lit = "true";  lit_len = 4; }
        else if (ch == 'f') { lit = "false"; lit_len = 5; }
        else { lit = "null"; lit_len = 4; }
        if ((size_t)(c->end - c->p) < lit_len || memcmp(c->p, lit, lit_len) != 0) {
            c->err = true;
            return false;
        }
        c->p += lit_len;
        return true;
    }
    double v;
    return read_number(c, &v);
}

/* --------------------------------------------------------------------- */
/* POST /summary                                                          */
/* --------------------------------------------------------------------- */

static esp_err_t reject_400(httpd_req_t *req, const char *route, const char *reason)
{
    ESP_LOGW(TAG, "rejecting %s: %s", route, reason);
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

    /* Atomic load-check-bind so two concurrent first-pushes can't both
     * win TOFU (deep-review H-1). */
    nvs_bind_result_t r = nvs_store_bind_or_check_client_id(agent, header);
    switch (r) {
    case NVS_BIND_OK_EXISTING:
        return true;
    case NVS_BIND_OK_NEW:
        /* Flip paired_<agent>=1 in the mDNS TXT so other clients on the
         * LAN stop offering this slot for pairing. */
        mdns_svc_refresh_paired(agent);
        /* Don't log the bound identifier (typically an email) at info
         * level — keep PII out of the serial log. Length is enough to
         * confirm a non-empty bind for diagnostics. */
        ESP_LOGI(TAG, "TOFU bound %s (id %u bytes)", agent, (unsigned)strlen(header));
        return true;
    case NVS_BIND_MISMATCH:
        ESP_LOGW(TAG, "/summary client-id mismatch for %s", agent);
        httpd_resp_set_status(req, "401 Unauthorized");
        httpd_resp_set_type(req, "application/json");
        const char *body = "{\"error\":\"client id mismatch\"}";
        httpd_resp_send(req, body, strlen(body));
        return false;
    case NVS_BIND_ERROR:
    default:
        ESP_LOGE(TAG, "nvs_store_bind_or_check_client_id(%s) failed", agent);
        httpd_resp_send_500(req);
        return false;
    }
}

/* Parse one session object. Accepts the keys in any order. The three
 * required keys (type, used_pct, resets_at) must be present; `rolling`
 * and `window_duration_mins` are optional and default to (false, 0) —
 * the conservative "fixed window, no synthesis" interpretation that
 * matches the wire-format contract for older daemons. */
static bool parse_session(cursor_t *c, session_snapshot_t *out)
{
    if (!eat(c, '{')) return false;
    bool have_type = false, have_pct = false, have_reset = false;
    memset(out, 0, sizeof(*out));

    for (;;) {
        skip_ws(c);
        if (c->p < c->end && *c->p == '}') { c->p++; break; }

        /* Buffer sized to hold the longest current key
         * (`window_duration_mins` = 20 chars) plus future headroom. */
        char key[32];
        if (!peek_key(c, key, sizeof(key))) return false;

        if (strcmp(key, "type") == 0) {
            if (!read_string(c, out->type, sizeof(out->type))) return false;
            /* Session type ends up rendered on the panel; reject control
             * bytes so a wire payload can't smuggle \n / \r / \t into
             * the LVGL label (deep-review L-1). */
            for (size_t k = 0; out->type[k] != '\0'; k++) {
                if ((unsigned char)out->type[k] < 0x20) {
                    c->err = true;
                    return false;
                }
            }
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
        } else if (strcmp(key, "rolling") == 0) {
            skip_ws(c);
            if (c->p + 4 <= c->end && memcmp(c->p, "true", 4) == 0) {
                out->rolling = true;
                c->p += 4;
            } else if (c->p + 5 <= c->end && memcmp(c->p, "false", 5) == 0) {
                out->rolling = false;
                c->p += 5;
            } else {
                c->err = true;
                return false;
            }
        } else if (strcmp(key, "window_duration_mins") == 0) {
            double v;
            if (!read_number(c, &v)) return false;
            /* Negative durations are nonsensical; clamp to 0 (meaning
             * "unknown / no synthesis"). Cap at INT32_MAX defensively
             * against pathological inputs. */
            if (v < 0.0) v = 0.0;
            if (v > (double)INT32_MAX) v = (double)INT32_MAX;
            out->window_duration_mins = (int32_t)v;
        } else {
            /* Unknown key — skip the value and continue. Forward-compat
             * for daemon-side wire-format additions; required keys above
             * are still enforced via have_* checks at the bottom. */
            if (!skip_value(c)) return false;
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
            /* Unknown top-level key — skip the value and continue. The
             * required keys above are still enforced by have_* checks at
             * the bottom. */
            if (!skip_value(c)) return false;
        }

        skip_ws(c);
        if (c->p < c->end && *c->p == ',') { c->p++; continue; }
        if (c->p < c->end && *c->p == '}') { c->p++; break; }
        c->err = true;
        return false;
    }

    return have_agent && have_captured && have_sessions && out->session_count > 0;
}

/* If the currently-running app is in PENDING_VERIFY (i.e. this boot is
 * the first one after an OTA), mark it valid so the bootloader stops
 * arming itself to roll back on the next reboot. Called from
 * summary_post_handler once a snapshot has been accepted end-to-end —
 * the strongest evidence we have that the firmware is healthy. Cheap:
 * after the first successful valid-mark per boot, this is a single
 * bool test. Transient errors (NULL partition, state read failure,
 * mark-valid failure) leave `s_done` unset so the next accepted
 * snapshot retries — otherwise a single flaky call would let the
 * bootloader roll back even though the firmware kept proving itself. */
static void maybe_mark_ota_valid(void)
{
    static bool s_done = false;
    if (s_done) return;
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) return;
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    if (esp_ota_get_state_partition(running, &state) != ESP_OK) return;
    if (state != ESP_OTA_IMG_PENDING_VERIFY) {
        /* Nothing to do this boot — latch so we don't keep peeking
         * at NVS on every successful snapshot. */
        s_done = true;
        return;
    }
    esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    if (err == ESP_OK) {
        s_done = true;
        ESP_LOGI(TAG, "OTA image marked valid; rollback canceled");
    } else {
        ESP_LOGW(TAG, "mark-valid failed: %s — will retry on next snapshot",
                 esp_err_to_name(err));
    }
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
        return reject_400(req, "/summary", "empty body");
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
        return reject_400(req, "/summary", "invalid AgentSnapshot");
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
        /* Strongest evidence the firmware is healthy: we accepted an
         * authorized snapshot end-to-end (radio + http + parser +
         * snapshot store). Cancel any pending rollback so a freshly-
         * flashed OTA image survives the next reboot. No-op once
         * already validated, and a no-op on every non-OTA boot. */
        maybe_mark_ota_valid();
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
                     "\",\"used_pct\":%.6g,\"resets_at\":%lld,"
                     "\"rolling\":%s,\"window_duration_mins\":%ld}",
                     (double)s->used_pct,
                     (long long)s->resets_at,
                     s->rolling ? "true" : "false",
                     (long)s->window_duration_mins);
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

    /* Constant-time match against every populated slot. We never break
     * early on a hit, so the wall-clock cost of a successful match is
     * the same as a successful mismatch — denies the LAN attacker a
     * timing channel for narrowing the bound client_id (deep-review
     * L-2). Whether a slot is *populated* is already leaked by
     * /health's own response body, so we don't try to hide that. */
    bool matched = false;
    for (size_t i = 0; i < KNOWN_AGENT_COUNT; ++i) {
        if (!slot_filled[i]) continue;
        size_t la = strnlen(slots[i], sizeof(slots[i]));
        size_t lb = strnlen(header, BURNSCOPE_CLIENT_ID_MAX);
        unsigned char diff = (la == lb) ? 0 : 1;
        size_t n = (la < lb) ? la : lb;
        for (size_t k = 0; k < n; ++k) {
            diff |= (unsigned char)slots[i][k] ^ (unsigned char)header[k];
        }
        bool slot_match = (la == lb) && (diff == 0);
        matched = matched || slot_match;
    }
    if (matched) return true;

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
/* POST /ota                                                              */
/* --------------------------------------------------------------------- */

/*
 * /ota authorization: stricter than /summary. We never bind a slot
 * here (no TOFU) — flashing arbitrary firmware on a freshly-booted
 * device just because someone POSTed first would be a trivially-
 * exploitable LAN attack. The caller must present an
 * `X-BurnScope-Client-Id` that already matches a populated slot.
 * Returns true → continue; false → 401/500 already sent on `req`.
 */
static bool authorize_ota(httpd_req_t *req)
{
    char slots[KNOWN_AGENT_COUNT][BURNSCOPE_CLIENT_ID_MAX];
    memset(slots, 0, sizeof(slots));
    bool slot_filled[KNOWN_AGENT_COUNT] = { false };
    int  filled_count = 0;
    for (size_t i = 0; i < KNOWN_AGENT_COUNT; ++i) {
        esp_err_t err = nvs_store_load_client_id(KNOWN_AGENTS[i], slots[i], sizeof(slots[i]));
        if (err == ESP_OK) {
            slot_filled[i] = true;
            filled_count++;
        } else if (err == ESP_ERR_NVS_NOT_FOUND) {
            /* Empty slot — leave slot_filled[i] = false. */
        } else {
            ESP_LOGE(TAG, "/ota: NVS read failed for %s: %s — failing closed",
                     KNOWN_AGENTS[i], esp_err_to_name(err));
            httpd_resp_send_500(req);
            return false;
        }
    }
    if (filled_count == 0) {
        ESP_LOGW(TAG, "/ota refused: device is not paired (no populated slots)");
        httpd_resp_set_status(req, "401 Unauthorized");
        httpd_resp_set_type(req, "application/json");
        const char *body = "{\"error\":\"device not paired; OTA requires an existing pairing\"}";
        httpd_resp_send(req, body, strlen(body));
        return false;
    }

    char header[BURNSCOPE_CLIENT_ID_MAX];
    memset(header, 0, sizeof(header));
    if (read_client_id_header(req, header, sizeof(header)) != 0 || header[0] == '\0') {
        ESP_LOGW(TAG, "/ota missing or oversized X-BurnScope-Client-Id");
        httpd_resp_set_status(req, "401 Unauthorized");
        httpd_resp_set_type(req, "application/json");
        const char *body = "{\"error\":\"client id required\"}";
        httpd_resp_send(req, body, strlen(body));
        return false;
    }

    /* Same constant-time match as /health: fixed-length XOR-compare
     * over zero-padded buffers, volatile accumulator so the compiler
     * can't short-circuit later iterations once a hit has been seen. */
    volatile unsigned int matched_acc = 0;
    for (size_t i = 0; i < KNOWN_AGENT_COUNT; ++i) {
        unsigned int slot_diff = 0;
        for (size_t k = 0; k < BURNSCOPE_CLIENT_ID_MAX; ++k) {
            slot_diff |= (unsigned char)slots[i][k] ^ (unsigned char)header[k];
        }
        unsigned int hit = (slot_diff == 0u) && slot_filled[i];
        matched_acc |= hit;
    }
    if (matched_acc) return true;

    ESP_LOGW(TAG, "/ota client-id matches no populated slot");
    httpd_resp_set_status(req, "401 Unauthorized");
    httpd_resp_set_type(req, "application/json");
    const char *body = "{\"error\":\"client id mismatch\"}";
    httpd_resp_send(req, body, strlen(body));
    return false;
}

/* esp_timer callback that triggers a soft reboot. Used to flush the
 * 202 response before the radio + filesystem go down. */
static void ota_reboot_cb(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "OTA committed; rebooting into the new image");
    esp_restart();
}

static esp_err_t ota_post_handler(httpd_req_t *req)
{
    if (!authorize_ota(req)) {
        return ESP_OK;
    }

    int content_len = req->content_len;
    if (content_len <= 0) {
        return reject_400(req, "/ota", "Content-Length required and > 0");
    }
    if (content_len > OTA_MAX_BODY) {
        ESP_LOGW(TAG, "/ota body too large (%d bytes)", content_len);
        httpd_resp_send_err(req, HTTPD_413_CONTENT_TOO_LARGE, "image too large");
        return ESP_OK;
    }

    /* Non-blocking mutex grab — if another upload is in flight, fail
     * fast with 409 rather than pile up two long-running uploads on
     * the httpd task pool. */
    if (xSemaphoreTake(s_ota_mutex, 0) != pdTRUE) {
        httpd_resp_set_status(req, "409 Conflict");
        httpd_resp_set_type(req, "application/json");
        const char *body = "{\"error\":\"another OTA already in progress\"}";
        httpd_resp_send(req, body, strlen(body));
        return ESP_OK;
    }

    const esp_partition_t *target = esp_ota_get_next_update_partition(NULL);
    if (target == NULL) {
        ESP_LOGE(TAG, "/ota: no inactive OTA partition found");
        xSemaphoreGive(s_ota_mutex);
        return httpd_resp_send_500(req);
    }
    if ((size_t)content_len > target->size) {
        ESP_LOGW(TAG, "/ota: image (%d B) exceeds slot size (%lu B)",
                 content_len, (unsigned long)target->size);
        xSemaphoreGive(s_ota_mutex);
        httpd_resp_send_err(req, HTTPD_413_CONTENT_TOO_LARGE,
                            "image larger than OTA slot");
        return ESP_OK;
    }

    esp_ota_handle_t handle = 0;
    esp_err_t err = esp_ota_begin(target, content_len, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(err));
        xSemaphoreGive(s_ota_mutex);
        return httpd_resp_send_500(req);
    }

    char *buf = malloc(OTA_RECV_CHUNK);
    if (buf == NULL) {
        esp_ota_abort(handle);
        xSemaphoreGive(s_ota_mutex);
        return httpd_resp_send_500(req);
    }

    int written = 0;
    while (written < content_len) {
        int want = content_len - written;
        if (want > OTA_RECV_CHUNK) want = OTA_RECV_CHUNK;
        int got = httpd_req_recv(req, buf, want);
        if (got <= 0) {
            if (got == HTTPD_SOCK_ERR_TIMEOUT) continue;
            ESP_LOGE(TAG, "/ota recv aborted after %d/%d bytes",
                     written, content_len);
            esp_ota_abort(handle);
            free(buf);
            xSemaphoreGive(s_ota_mutex);
            return ESP_FAIL;
        }
        err = esp_ota_write(handle, buf, got);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "esp_ota_write failed at %d B: %s",
                     written, esp_err_to_name(err));
            esp_ota_abort(handle);
            free(buf);
            xSemaphoreGive(s_ota_mutex);
            /* esp_ota_write performs magic-byte + header validation
             * on the first chunk. A bad image is the client's fault,
             * not ours — return 400 so the daemon doesn't retry it. */
            if (err == ESP_ERR_OTA_VALIDATE_FAILED) {
                return reject_400(req, "/ota", "image rejected by bootloader");
            }
            return httpd_resp_send_500(req);
        }
        written += got;
    }
    free(buf);

    err = esp_ota_end(handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_end failed: %s — image rejected", esp_err_to_name(err));
        xSemaphoreGive(s_ota_mutex);
        /* esp_ota_end rejects images that fail the magic-byte check or
         * sha256 verification. A 400 reads more honest than 500 here:
         * the *server* is fine, the *image* the client sent isn't. */
        return reject_400(req, "/ota", "image rejected by bootloader");
    }

    err = esp_ota_set_boot_partition(target);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_set_boot_partition failed: %s", esp_err_to_name(err));
        xSemaphoreGive(s_ota_mutex);
        return httpd_resp_send_500(req);
    }

    /* Tell the client what's about to happen before the radio dies.
     * We don't release s_ota_mutex on the success path on purpose —
     * the next reboot reinitialises it, and holding it during the
     * ~1 s grace period prevents a racing second uploader from
     * starting an OTA into a partition we just sealed. */
    ESP_LOGI(TAG, "OTA accepted: %d B written to %s; reboot in ~1 s",
             written, target->label);
    httpd_resp_set_status(req, "202 Accepted");
    httpd_resp_set_type(req, "application/json");
    char resp[128];
    int rn = snprintf(resp, sizeof(resp),
                      "{\"status\":\"flashing\",\"bytes\":%d,\"next_boot\":\"%s\"}",
                      written, target->label);
    if (rn < 0 || (size_t)rn >= sizeof(resp)) {
        httpd_resp_send(req, "{\"status\":\"flashing\"}", strlen("{\"status\":\"flashing\"}"));
    } else {
        httpd_resp_send(req, resp, rn);
    }

    /* Schedule the reboot ~1 s out so the 202 has time to flush
     * through the socket. esp_timer runs on its own task so it
     * isn't blocked by the httpd handler returning. */
    const esp_timer_create_args_t targs = {
        .callback = ota_reboot_cb,
        .name     = "ota_reboot",
    };
    esp_timer_handle_t t = NULL;
    if (esp_timer_create(&targs, &t) == ESP_OK) {
        esp_timer_start_once(t, 1000 * 1000);
    } else {
        /* Timer creation shouldn't fail; if it does, reboot immediately
         * rather than leaving the device in a half-committed state. */
        esp_restart();
    }
    return ESP_OK;
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

    if (s_ota_mutex == NULL) {
        s_ota_mutex = xSemaphoreCreateMutex();
        /* Failure to create the mutex would leave /ota racing on its
         * own — refuse to bring the server up rather than ship that. */
        ESP_ERROR_CHECK(s_ota_mutex != NULL ? ESP_OK : ESP_ERR_NO_MEM);
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

    const httpd_uri_t ota = {
        .uri = "/ota", .method = HTTP_POST, .handler = ota_post_handler,
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_server, &ota));

    ESP_LOGI(TAG, "HTTP server listening on :80");
}
