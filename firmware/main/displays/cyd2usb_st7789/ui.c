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

    lv_obj_t *bar = lv_bar_create(card);
    lv_obj_set_size(bar, 292, 15);
    lv_obj_align(bar, LV_ALIGN_TOP_LEFT, 0, 34);
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

    /* Two body rows. Header takes ~40 px, leaves 200 px for two rows
     * separated by a small gap. */
    const int rows = 2;
    const int top  = 40;
    const int gap  = 8;
    const int row_h = (240 - top - gap * (rows + 1)) / rows;  /* ~88 px */
    for (int i = 0; i < rows; ++i) {
        int y = top + gap + i * (row_h + gap);
        build_row(scr, i, y, row_h);
    }
    /* Stash any unused row pointers so callers never deref them. */
    for (int i = rows; i < SNAPSHOT_MAX_SESSIONS; ++i) {
        s_rows[i].card = NULL;
    }

    s_agent_screen = scr;
}

static void render_snapshot_locked(const agent_snapshot_t *snap)
{
    /* Header icon + label. */
    lv_image_set_src(s_agent_icon, agent_icon(snap->agent));
    lv_label_set_text(s_agent_label, "Usage");

    const agent_palette_t *pal = palette_for(snap->agent);

    /* Pull wall clock once per repaint. May be 0 before SNTP completes;
     * the tick will pick up the right values once the clock is set. */
    int64_t now = (int64_t)time(NULL);

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
        float pct = s->used_pct;
        if (pct < 0.0f) pct = 0.0f;
        if (pct > 1.0f) pct = 1.0f;

        lv_label_set_text(r->type_lbl, s->type);
        char pct_buf[8];
        snprintf(pct_buf, sizeof(pct_buf), "%d%%", (int)lroundf(pct * 100.0f));
        lv_label_set_text(r->pct_lbl, pct_buf);
        lv_bar_set_value(r->bar, (int)lroundf(pct * 1000.0f), LV_ANIM_OFF);
        lv_obj_set_style_bg_color(r->bar, lv_color_hex(pal->rows[i]), LV_PART_INDICATOR);

        char buf[32];
        format_countdown(s->resets_at - now, buf, sizeof(buf));
        lv_label_set_text(r->countdown_lbl, buf);
    }
}

static void show_agent_locked(const agent_snapshot_t *snap)
{
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
    show_agent_locked(snap);
    lvgl_port_unlock();
}
