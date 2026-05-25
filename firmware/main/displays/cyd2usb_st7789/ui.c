/*
 * cyd2usb_st7789 LVGL UI.
 *
 * Two screens:
 *   - Splash: title + status line + version footer. Used during boot,
 *     provisioning, and "waiting for daemon".
 *   - Agent:  FSD §6.1.6 layout — header (logo placeholder, "Usage",
 *     hidden battery), two rounded rows each carrying a type tag, an
 *     integer percentage, a horizontal progress bar (filled = used_pct
 *     per FR-4.5), and a "resets in …" countdown label.
 *
 * A 1 Hz LVGL timer (installed in `display_profile_init`) calls
 * `display_profile_tick` which recomputes countdowns and, when ≥2 agents
 * are stored, advances a slow cycling selection (FR-4.10).
 *
 * All LVGL mutation goes through `lvgl_port_lock`; the profile is the only
 * translation unit that talks LVGL.
 */

#include "display_profile.h"

#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "lvgl.h"

#include "driver.h"
#include "nvs_store.h"
#include "snapshot.h"
#include "version.h"

/* Per-agent brand icons (24x24 ARGB8888). Sources are the official brand
 * marks mirrored by lobehub/lobe-icons (MIT); icons/icon_*.c is regenerated
 * with rsvg-convert + LVGL's LVGLImage.py — see firmware README. */
LV_IMAGE_DECLARE(icon_claude);
LV_IMAGE_DECLARE(icon_codex);

static const char *TAG = "render";

/* Splash widgets. */
static lv_obj_t *s_splash_screen = NULL;
static lv_obj_t *s_status_label  = NULL;

/* Agent-screen widgets (built once, mutated per snapshot). */
static lv_obj_t *s_agent_screen  = NULL;
static lv_obj_t *s_agent_icon    = NULL;     /* header brand icon (lv_image) */
static lv_obj_t *s_agent_label   = NULL;     /* "Usage" or agent name */
static lv_obj_t *s_footer_left   = NULL;     /* bound client_id (subtle gray) */
static lv_obj_t *s_footer_right  = NULL;     /* "updated N min ago" (relative) */
typedef struct {
    lv_obj_t *card;
    lv_obj_t *type_lbl;
    lv_obj_t *pct_lbl;
    lv_obj_t *bar;
    lv_obj_t *countdown_lbl;
} ui_row_t;
static ui_row_t s_rows[SNAPSHOT_MAX_SESSIONS];

/* The agent we are currently rendering ("" when on splash). */
static char s_visible_agent[SNAPSHOT_AGENT_MAX] = "";

/*
 * Cached client-id per known agent, fetched from NVS once per visible
 * cycle. We intentionally never read NVS from the LVGL render path —
 * `refresh_footer_cid` runs only when we swap to a new agent.
 * Index 0 = "claude", 1 = "codex"; mirrors the agent name array below.
 */
static const char *const FOOTER_AGENTS[] = { "claude", "codex" };
#define FOOTER_AGENT_COUNT (sizeof(FOOTER_AGENTS) / sizeof(FOOTER_AGENTS[0]))
static char s_footer_cid[FOOTER_AGENT_COUNT][BURNSCOPE_CLIENT_ID_MAX];

static int footer_agent_idx(const char *agent)
{
    for (size_t i = 0; i < FOOTER_AGENT_COUNT; ++i) {
        if (strcmp(agent, FOOTER_AGENTS[i]) == 0) return (int)i;
    }
    return -1;
}

static void refresh_footer_cid(const char *agent)
{
    int idx = footer_agent_idx(agent);
    if (idx < 0) return;
    s_footer_cid[idx][0] = '\0';
    (void)nvs_store_load_client_id(agent, s_footer_cid[idx], sizeof(s_footer_cid[idx]));
}

static const char *footer_cid_for(const char *agent)
{
    int idx = footer_agent_idx(agent);
    if (idx < 0) return "";
    return s_footer_cid[idx];
}

/* Cycling timer state (1 tick = 1 s; cycle every 5 s with ≥2 agents). */
#define CYCLE_INTERVAL_S 5
static int s_cycle_ticks = 0;

/* Per-agent bar palette (FR-4.7). One row of colours per agent; unknown
 * agents fall back to the first entry (claude). `lv_color_hex` isn't a
 * constant expression, so we keep colours as 0xRRGGBB ints and convert
 * at use. */
typedef struct {
    const char *agent;
    uint32_t rows[SNAPSHOT_MAX_SESSIONS];
} agent_palette_t;

static const agent_palette_t AGENT_PALETTES[] = {
    { "claude", { 0xDE7356, 0xA4A049, 0xB0BEC5 } },
    { "codex",  { 0x81C3DD, 0xA4A049, 0xB0BEC5 } },
};

static const agent_palette_t *palette_for(const char *agent)
{
    for (size_t i = 0; i < sizeof(AGENT_PALETTES) / sizeof(AGENT_PALETTES[0]); ++i) {
        if (strcmp(agent, AGENT_PALETTES[i].agent) == 0) {
            return &AGENT_PALETTES[i];
        }
    }
    return &AGENT_PALETTES[0];
}

/* Map an agent name to its brand icon. Unknown agents fall back to the
 * Claude mark so the header stays populated rather than blank. */
static const lv_image_dsc_t *agent_icon(const char *agent)
{
    if (strcmp(agent, "codex") == 0) return &icon_codex;
    return &icon_claude;
}

static void format_countdown(int64_t seconds, char *out, size_t n)
{
    if (seconds < 0) {
        snprintf(out, n, "reset due");
        return;
    }
    int64_t s = seconds;
    int days  = (int)(s / 86400); s %= 86400;
    int hours = (int)(s / 3600);  s %= 3600;
    int mins  = (int)(s / 60);
    if (days > 0) {
        snprintf(out, n, "resets in %dd %02dh", days, hours);
    } else if (hours > 0) {
        snprintf(out, n, "resets in %dh %02dm", hours, mins);
    } else {
        snprintf(out, n, "resets in %dm %02ds", mins, (int)(s % 60));
    }
}

static void build_splash(lv_display_t *disp)
{
    lv_obj_t *scr = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);
    lv_obj_set_style_text_color(scr, lv_color_white(), 0);

    lv_obj_t *title = lv_label_create(scr);
    lv_label_set_text(title, "BurnScope");
    lv_obj_set_style_text_font(title, &lv_font_montserrat_24, 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 16);

    s_status_label = lv_label_create(scr);
    lv_label_set_long_mode(s_status_label, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(s_status_label, 300);
    lv_obj_set_style_text_align(s_status_label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_font(s_status_label, &lv_font_montserrat_24, 0);
    lv_label_set_text(s_status_label, "Booting...");
    lv_obj_center(s_status_label);

    lv_obj_t *version = lv_label_create(scr);
    lv_label_set_text(version, "v" BURNSCOPE_FW_VERSION);
    lv_obj_set_style_text_color(version, lv_color_hex(0x808080), 0);
    lv_obj_align(version, LV_ALIGN_BOTTOM_LEFT, 8, -6);

    s_splash_screen = scr;
    lv_screen_load(s_splash_screen);
    (void)disp;
}

static void build_row(lv_obj_t *parent, int row_idx, int y_offset, int height)
{
    ui_row_t *r = &s_rows[row_idx];

    lv_obj_t *card = lv_obj_create(parent);
    lv_obj_set_size(card, 304, height);
    lv_obj_align(card, LV_ALIGN_TOP_MID, 0, y_offset);
    lv_obj_set_style_bg_color(card, lv_color_hex(0x1C1C1C), 0);
    lv_obj_set_style_border_width(card, 0, 0);
    lv_obj_set_style_radius(card, 8, 0);
    lv_obj_set_style_pad_all(card, 6, 0);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

    /* Warm off-white shared by the chip and the percent — keeps the row
     * label and the percentage visually paired. */
    const lv_color_t LABEL_FG = lv_color_hex(0xF9F2DF);
    /* Chip background sits one step lighter than the card (0x1C1C1C). */
    const lv_color_t CHIP_BG    = lv_color_hex(0x2E2E2E);

    /* Type tag rendered as a rounded chip with the serif face. */
    lv_obj_t *type_lbl = lv_label_create(card);
    lv_label_set_text(type_lbl, "—");
    lv_obj_set_style_text_font(type_lbl, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(type_lbl, LABEL_FG, 0);
    lv_obj_set_style_bg_color(type_lbl, CHIP_BG, 0);
    lv_obj_set_style_bg_opa(type_lbl, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(type_lbl, 6, 0);
    lv_obj_set_style_pad_hor(type_lbl, 8, 0);
    lv_obj_set_style_pad_ver(type_lbl, 2, 0);
    lv_obj_align(type_lbl, LV_ALIGN_TOP_LEFT, 0, 0);

    lv_obj_t *pct_lbl = lv_label_create(card);
    lv_label_set_text(pct_lbl, "0%");
    lv_obj_set_style_text_font(pct_lbl, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(pct_lbl, LABEL_FG, 0);
    lv_obj_align(pct_lbl, LV_ALIGN_TOP_RIGHT, 0, 2);

    /* Bar Y in the card's content area, dialled in via font-preview.html. */
    lv_obj_t *bar = lv_bar_create(card);
    lv_obj_set_size(bar, 292, 15);
    lv_obj_align(bar, LV_ALIGN_TOP_LEFT, 0, 32);
    lv_bar_set_range(bar, 0, 1000);
    lv_bar_set_value(bar, 0, LV_ANIM_OFF);
    /* Explicit bg_opa + zero border on both parts: the default theme can
     * leave PART_MAIN transparent, which kills the visible track. */
    lv_obj_set_style_bg_color(bar, lv_color_hex(0x2F2F2F), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(bar, 0, LV_PART_MAIN);
    /* Indicator colour is set per-agent in render_snapshot_locked. */
    lv_obj_set_style_bg_opa(bar, LV_OPA_COVER, LV_PART_INDICATOR);
    lv_obj_set_style_border_width(bar, 0, LV_PART_INDICATOR);
    lv_obj_set_style_radius(bar, 3, LV_PART_MAIN);
    lv_obj_set_style_radius(bar, 3, LV_PART_INDICATOR);

    lv_obj_t *countdown_lbl = lv_label_create(card);
    lv_label_set_text(countdown_lbl, "resets in --");
    lv_obj_set_style_text_font(countdown_lbl, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(countdown_lbl, lv_color_hex(0xB0ACA0), 0);
    lv_obj_align(countdown_lbl, LV_ALIGN_BOTTOM_LEFT, 0, 0);

    r->card = card;
    r->type_lbl = type_lbl;
    r->pct_lbl = pct_lbl;
    r->bar = bar;
    r->countdown_lbl = countdown_lbl;
}

static void build_agent_screen(void)
{
    lv_obj_t *scr = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);
    lv_obj_set_style_text_color(scr, lv_color_hex(0xF9F2DF), 0);
    lv_obj_set_style_pad_all(scr, 0, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    /* Header: 24x24 agent brand icon top-left, "Usage" centred, battery
     * slot top-right reserved but hidden (has_battery == false on CYD).
     *
     * Optical alignment: Montserrat 28's bbox is 30 px tall with the
     * baseline 25 px below its top and cap-top ~5 px below its top, so the
     * caps of "Usage" sit centred around y=21. The icon's visible-pixel
     * centroid lands at +11.4 from its own top, so placing the icon at
     * y=10 puts its optical centre at ~21 too — matching the text. */
    s_agent_icon = lv_image_create(scr);
    lv_image_set_src(s_agent_icon, &icon_claude);
    lv_obj_align(s_agent_icon, LV_ALIGN_TOP_LEFT, 8, 10);

    s_agent_label = lv_label_create(scr);
    lv_label_set_text(s_agent_label, "Usage");
    lv_obj_set_style_text_font(s_agent_label, &lv_font_montserrat_28, 0);
    lv_obj_align(s_agent_label, LV_ALIGN_TOP_MID, 0, 6);

    /* Two body rows. Header is 40 px at the top; a 14 px footer band
     * sits near the bottom for owner-id + updated timestamp. Vertical
     * layout (dialled in via firmware/scripts/font-preview.html):
     *
     *   [40 header][8][row][8][row][5][14 footer][3 bottom-margin] == 240
     *
     * → row_h = (240 - 40 - 2*inter_gap - footer_top_margin
     *           - footer_h - footer_bottom_margin) / 2  = 81 px.
     *
     * The preview tool's "ideal" footer-bottom-margin was 2 px (which
     * would give row_h = 81.5); we widen it to 3 px so row_h lands on
     * an exact integer. The 1 px shift below the footer is invisible.
     */
    const int rows                 = 2;
    const int top                  = 40;
    const int inter_gap            = 8;
    const int footer_h             = 14;
    const int footer_top_margin    = 5;
    const int footer_bottom_margin = 3;
    const int row_h = (240 - top - inter_gap * rows
                       - footer_top_margin - footer_h - footer_bottom_margin) / rows;
    for (int i = 0; i < rows; ++i) {
        int y = top + inter_gap + i * (row_h + inter_gap);
        build_row(scr, i, y, row_h);
    }
    /* Stash any unused row pointers so callers never deref them. */
    for (int i = rows; i < SNAPSHOT_MAX_SESSIONS; ++i) {
        s_rows[i].card = NULL;
    }

    /* Footer band. Warm off-white that sits in the same family as the
     * row labels — readable but smaller than the body type, so the eye
     * lands on the percentages first. The right-hand timestamp formatter
     * renders the snapshot age as a relative phrase ("N min ago"), so
     * firmware never needs to care about the user's timezone — only the
     * delta between two unix timestamps matters. Truncation on the left
     * label is handled by LVGL: dots mode replaces the overflow with an
     * ellipsis when the label exceeds its width. */
    const lv_color_t FOOTER_FG = lv_color_hex(0x5C5C5C);

    s_footer_left = lv_label_create(scr);
    lv_obj_set_width(s_footer_left, 184);
    lv_label_set_long_mode(s_footer_left, LV_LABEL_LONG_DOT);
    lv_label_set_text(s_footer_left, "unpaired");
    lv_obj_set_style_text_font(s_footer_left, &lv_font_montserrat_10, 0);
    lv_obj_set_style_text_color(s_footer_left, FOOTER_FG, 0);
    lv_obj_align(s_footer_left, LV_ALIGN_BOTTOM_LEFT, 8, -footer_bottom_margin);

    s_footer_right = lv_label_create(scr);
    lv_label_set_text(s_footer_right, "");
    lv_obj_set_style_text_font(s_footer_right, &lv_font_montserrat_10, 0);
    lv_obj_set_style_text_color(s_footer_right, FOOTER_FG, 0);
    lv_obj_align(s_footer_right, LV_ALIGN_BOTTOM_RIGHT, -8, -footer_bottom_margin);

    s_agent_screen = scr;
}

/*
 * Format the age of `captured_at` (unix seconds) into `out` as a short
 * relative phrase: "updated <1 min ago", "updated N min ago", "updated
 * N hr ago", or "updated N days ago". Relative phrasing means we never
 * have to know the user's timezone — only the delta between two unix
 * timestamps matters. When either clock is unsynced (captured_at or
 * the local wall clock < 2023-11-14) we write the empty string so the
 * footer right-half stays clean. A negative delta (snapshot slightly
 * in the future from clock skew) is rendered as "<1 min ago" rather
 * than something nonsensical. */
static void format_updated_relative(int64_t captured_at, char *out, size_t n)
{
    if (n == 0) return;
    out[0] = '\0';
    if (captured_at < 1700000000) {
        return;
    }
    int64_t now = (int64_t)time(NULL);
    if (now < 1700000000) {
        return;
    }
    int64_t delta = now - captured_at;
    if (delta < 60) {
        snprintf(out, n, "updated <1 min ago");
    } else if (delta < 3600) {
        snprintf(out, n, "updated %d min ago", (int)(delta / 60));
    } else if (delta < 86400) {
        snprintf(out, n, "updated %d hr ago", (int)(delta / 3600));
    } else {
        snprintf(out, n, "updated %d days ago", (int)(delta / 86400));
    }
}

static void render_footer_locked(const agent_snapshot_t *snap)
{
    if (s_footer_left == NULL || s_footer_right == NULL) return;
    const char *cid = footer_cid_for(snap->agent);
    if (cid[0] == '\0') {
        /* No binding yet — render the "unpaired" placeholder and clear
         * the timestamp. The wire spec lets v2 clients pair via TOFU,
         * so this state is transient on a fresh device. */
        lv_label_set_text(s_footer_left, "unpaired");
        lv_label_set_text(s_footer_right, "");
        return;
    }
    lv_label_set_text(s_footer_left, cid);

    char buf[32];
    format_updated_relative(snap->captured_at, buf, sizeof(buf));
    lv_label_set_text(s_footer_right, buf);
}

static void render_snapshot_locked(const agent_snapshot_t *snap)
{
    /* Header icon + label. */
    lv_image_set_src(s_agent_icon, agent_icon(snap->agent));
    lv_label_set_text(s_agent_label, "Usage");

    render_footer_locked(snap);

    const agent_palette_t *pal = palette_for(snap->agent);

    /* Pull wall clock once per repaint. Before SNTP completes time(NULL)
     * is ~0 (seconds since boot), which would render countdowns like
     * "resets in 20000d". Treat anything before 2023-11-14 as unsynced
     * and substitute a placeholder until NTP catches up. */
    int64_t now = (int64_t)time(NULL);
    const bool clock_synced = now > 1700000000;

    for (int i = 0; i < SNAPSHOT_MAX_SESSIONS; ++i) {
        ui_row_t *r = &s_rows[i];
        if (r->card == NULL) {
            continue;
        }
        if (i >= snap->session_count) {
            lv_obj_add_flag(r->card, LV_OBJ_FLAG_HIDDEN);
            continue;
        }
        lv_obj_clear_flag(r->card, LV_OBJ_FLAG_HIDDEN);

        const session_snapshot_t *s = &snap->sessions[i];
        /* Synthesise the reset time locally for rolling windows at idle
         * (codex). The pushed `resets_at` would otherwise be stale —
         * see _anchor_resets_at in the codex daemon and the helper in
         * snapshot.h. For fixed-window agents (claude) this is a no-op. */
        int64_t reset_at = effective_resets_at(s, now);
        float pct = s->used_pct;
        if (pct < 0.0f) pct = 0.0f;
        if (pct > 1.0f) pct = 1.0f;
        /* Post-reset auto-zero: once the wall clock crosses the
         * effective reset boundary the old window is logically gone.
         * Keep the bar at 0 until the next push delivers the new
         * window's used_pct + resets_at. The store is intentionally not
         * mutated — /health still reports what the daemon last sent,
         * so its drift-detection stays meaningful. */
        if (clock_synced && now >= reset_at) {
            pct = 0.0f;
        }

        lv_label_set_text(r->type_lbl, s->type);
        char pct_buf[8];
        snprintf(pct_buf, sizeof(pct_buf), "%d%%", (int)lroundf(pct * 100.0f));
        lv_label_set_text(r->pct_lbl, pct_buf);
        lv_bar_set_value(r->bar, (int)lroundf(pct * 1000.0f), LV_ANIM_OFF);
        lv_obj_set_style_bg_color(r->bar, lv_color_hex(pal->rows[i]), LV_PART_INDICATOR);

        char buf[32];
        if (clock_synced) {
            format_countdown(reset_at - now, buf, sizeof(buf));
        } else {
            snprintf(buf, sizeof(buf), "syncing...");
        }
        lv_label_set_text(r->countdown_lbl, buf);
    }
}

static void show_agent_locked(const agent_snapshot_t *snap)
{
    /* Reload the per-agent client-id from NVS on the swap boundary —
     * cheap (single key read) and avoids touching NVS from the 1 Hz
     * tick. Stale only between a binding-change and the next swap,
     * which is acceptable for a footer label. */
    refresh_footer_cid(snap->agent);
    render_snapshot_locked(snap);
    strncpy(s_visible_agent, snap->agent, sizeof(s_visible_agent) - 1);
    s_visible_agent[sizeof(s_visible_agent) - 1] = '\0';
    lv_screen_load(s_agent_screen);
}

/* Cycling helper: snapshot_store_foreach callback that fills a small
 * static array so we can pick the "next" agent after the visible one. */
typedef struct {
    agent_snapshot_t items[2];
    int count;
} cycle_buf_t;

static void cycle_collect(const agent_snapshot_t *snap, void *user)
{
    cycle_buf_t *buf = (cycle_buf_t *)user;
    if (buf->count < 2) {
        buf->items[buf->count++] = *snap;
    }
}

/* Advance to the snapshot after the one currently visible (wrap around).
 * Leaves the screen unchanged when fewer than 2 agents are stored. */
static void advance_to_next_agent_locked(void)
{
    cycle_buf_t buf = { .count = 0 };
    snapshot_store_foreach(cycle_collect, &buf);
    if (buf.count < 2) {
        return;
    }
    for (int i = 0; i < buf.count; ++i) {
        if (strcmp(buf.items[i].agent, s_visible_agent) == 0) {
            show_agent_locked(&buf.items[(i + 1) % buf.count]);
            return;
        }
    }
    /* Visible agent no longer in the store — pick the first one. */
    show_agent_locked(&buf.items[0]);
}

static void tick_lvgl_cb(lv_timer_t *t)
{
    (void)t;
    /* Called from inside the LVGL task with the lock already held. */
    if (s_visible_agent[0] == '\0') {
        return;
    }
    agent_snapshot_t cur;
    if (snapshot_store_get(s_visible_agent, &cur)) {
        render_snapshot_locked(&cur);
    }
    if (snapshot_store_count() >= 2) {
        s_cycle_ticks++;
        if (s_cycle_ticks >= CYCLE_INTERVAL_S) {
            s_cycle_ticks = 0;
            advance_to_next_agent_locked();
        }
    } else {
        s_cycle_ticks = 0;
    }
}

void display_profile_init(void)
{
    lv_display_t *disp = cyd2usb_st7789_driver_init();

    if (!lvgl_port_lock(0)) {
        ESP_LOGE(TAG, "lvgl_port_lock failed during init");
        return;
    }
    build_splash(disp);
    build_agent_screen();
    lv_timer_create(tick_lvgl_cb, 1000, NULL);
    lvgl_port_unlock();
}

void display_profile_show_status(const char *text)
{
    if (s_status_label == NULL) {
        return;
    }
    if (!lvgl_port_lock(0)) {
        ESP_LOGE(TAG, "lvgl_port_lock failed in show_status");
        return;
    }
    lv_label_set_text(s_status_label, text != NULL ? text : "");
    s_visible_agent[0] = '\0';
    lv_screen_load(s_splash_screen);
    lvgl_port_unlock();
}

void display_profile_show_agent(const agent_snapshot_t *snap)
{
    if (snap == NULL) {
        display_profile_show_status("Waiting for daemon...");
        return;
    }
    if (!lvgl_port_lock(0)) {
        ESP_LOGE(TAG, "lvgl_port_lock failed in show_agent");
        return;
    }
    /* Only force a screen swap when we're still on the splash — that's the
     * "first push" transition out of "Waiting for daemon...". Subsequent
     * pushes just refresh the snapshot store (already done by the caller);
     * the 1 Hz tick picks up new values and handles agent cycling, so
     * arrivals from a different agent must not yank the rotation. */
    if (s_visible_agent[0] == '\0') {
        show_agent_locked(snap);
    }
    lvgl_port_unlock();
}
