/*
 * amoled_co5300 LVGL UI — concentric-arc dial on a 466×466 round AMOLED.
 *
 * Two screens, matching the cyd2usb profile:
 *   - Splash: title + status line + version footer. Used during boot,
 *     provisioning, and "waiting for daemon".
 *   - Agent:  three concentric rings (one per session, outer = row 0)
 *     orbiting a central core that carries the agent brand icon, the
 *     row-0 percentage in display-size type, and the row-0 type tag.
 *     R1/R2 type tags + countdowns sit as chip pills in the 90° gap
 *     at the top of the dial. Footer (client_id + relative timestamp)
 *     sits inside the innermost ring.
 *
 * Layout constants below are the defaults from
 * `docs/ui/amoled_co5300.md`. The HTML tuner at
 * `firmware/scripts/amoled-preview.html` lets you dial them in
 * pre-board and emits a #define block you paste here.
 *
 * All LVGL mutation goes through `lvgl_port_lock`. Like the cyd2usb
 * profile, this is the only translation unit that talks LVGL.
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

/* Per-agent brand icons. Reuses the 24×24 ARGB8888 assets that ship
 * with the cyd2usb profile — LVGL upscales them in-place on the
 * larger AMOLED. Regenerate at 48×48 for crisper rendering when
 * dropping the 24 px assets becomes acceptable (see design spec §7). */
LV_IMAGE_DECLARE(icon_claude);
LV_IMAGE_DECLARE(icon_codex);

static const char *TAG = "render";

/* ===== Layout constants — paste from amoled-preview.html ============= */
#define AMOLED_CX                233
#define AMOLED_CY                233
#define AMOLED_SAFE_RADIUS       220

#define AMOLED_R0_OUTER          215
#define AMOLED_R0_INNER          197
#define AMOLED_R1_OUTER          192
#define AMOLED_R1_INNER          178
#define AMOLED_R2_OUTER          173
#define AMOLED_R2_INNER          161

#define AMOLED_ARC_START_DEG     135
#define AMOLED_ARC_SWEEP_DEG     270
#define AMOLED_ARC_VALUE_RANGE   1000   /* matches cyd2usb bar resolution */

#define AMOLED_CORE_ICON_SIZE    24     /* using existing 24px icons */
#define AMOLED_CORE_ICON_Y       188
#define AMOLED_CORE_PRIMARY_Y    248
#define AMOLED_CORE_SECONDARY_Y  282

#define AMOLED_HEADER_Y          14

#define AMOLED_FOOTER_Y          410
#define AMOLED_FOOTER_LEFT_X     48
#define AMOLED_FOOTER_RIGHT_X    418
#define AMOLED_FOOTER_LEFT_WIDTH 180

#define AMOLED_PILL_Y            36
#define AMOLED_PILL_R1_X         170
#define AMOLED_PILL_R2_X         296
#define AMOLED_PILL_CHIP_RADIUS  6
#define AMOLED_PILL_PAD_H        8
#define AMOLED_PILL_PAD_V        2
#define AMOLED_PILL_CD_OFFSET_X  60

/* ===== Splash widgets =============================================== */
static lv_obj_t *s_splash_screen = NULL;
static lv_obj_t *s_status_label  = NULL;

/* ===== Agent-screen widgets (built once, mutated per snapshot) ====== */
static lv_obj_t *s_agent_screen     = NULL;
static lv_obj_t *s_header_label     = NULL;  /* "Usage" */
static lv_obj_t *s_core_icon        = NULL;  /* agent brand */
static lv_obj_t *s_core_primary     = NULL;  /* row-0 percent, big */
static lv_obj_t *s_core_secondary   = NULL;  /* row-0 type tag */
static lv_obj_t *s_footer_left      = NULL;  /* client_id */
static lv_obj_t *s_footer_right     = NULL;  /* "updated N min ago" */

typedef struct {
    lv_obj_t *arc;              /* indicator + track */
    lv_obj_t *pill_tag;         /* R1/R2 only — NULL for R0 (in-core) */
    lv_obj_t *pill_countdown;   /* R1/R2 only — NULL for R0 */
} ui_ring_t;
static ui_ring_t s_rings[SNAPSHOT_MAX_SESSIONS];

/* The agent we are currently rendering ("" when on splash). */
static char s_visible_agent[SNAPSHOT_AGENT_MAX] = "";

/* Cached client-id per known agent — same NVS-on-swap-boundary
 * pattern as the cyd2usb profile so we never touch NVS from the
 * 1 Hz tick. */
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

/* Per-agent ring palette (FR-4.7). Identical to the cyd2usb profile so
 * a multi-screen setup looks unified. */
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

static const lv_image_dsc_t *agent_icon(const char *agent)
{
    if (strcmp(agent, "codex") == 0) return &icon_codex;
    return &icon_claude;
}

/* Identical countdown formatter to the cyd2usb profile — per FR-5
 * each profile owns its UI, including helpers. */
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
        snprintf(out, n, "%dd %02dh", days, hours);
    } else if (hours > 0) {
        snprintf(out, n, "%dh %02dm", hours, mins);
    } else {
        snprintf(out, n, "%dm %02ds", mins, (int)(s % 60));
    }
}

/* Format the age of `captured_at` as a relative phrase. Identical
 * semantics to the cyd2usb profile (see comment there for the
 * timezone-free justification). */
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

/* Place a widget at (x, y) on the panel by aligning to top-left and
 * offsetting. Centres the widget on (x, y) when `center` is true. */
static void place_at(lv_obj_t *obj, int x, int y, bool center)
{
    if (center) {
        /* For centring, defer to LVGL's content size + LV_ALIGN_CENTER. */
        lv_obj_align(obj, LV_ALIGN_TOP_LEFT, 0, 0);
        lv_obj_update_layout(obj);
        const int w = lv_obj_get_width(obj);
        const int h = lv_obj_get_height(obj);
        lv_obj_align(obj, LV_ALIGN_TOP_LEFT, x - w / 2, y - h / 2);
    } else {
        lv_obj_align(obj, LV_ALIGN_TOP_LEFT, x, y);
    }
}

/* ===== Splash ======================================================= */

static void build_splash(lv_display_t *disp)
{
    lv_obj_t *scr = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);
    lv_obj_set_style_text_color(scr, lv_color_hex(0xF9F2DF), 0);
    lv_obj_set_style_pad_all(scr, 0, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *title = lv_label_create(scr);
    lv_label_set_text(title, "BurnScope");
    lv_obj_set_style_text_font(title, &lv_font_montserrat_28, 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 180 - 14);  /* y=180 centred */

    s_status_label = lv_label_create(scr);
    lv_label_set_long_mode(s_status_label, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(s_status_label, 360);
    lv_obj_set_style_text_align(s_status_label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_font(s_status_label, &lv_font_montserrat_24, 0);
    lv_label_set_text(s_status_label, "Booting...");
    lv_obj_align(s_status_label, LV_ALIGN_TOP_MID, 0, 233 - 12);

    lv_obj_t *version = lv_label_create(scr);
    lv_label_set_text(version, "v" BURNSCOPE_FW_VERSION);
    lv_obj_set_style_text_font(version, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(version, lv_color_hex(0x808080), 0);
    /* y=410 — same band as the agent-screen footer. Provisioning's
     * three-line splash ("Setup mode / Join … / Open 192.168.4.1") at
     * M24 overruns the spec'd y=300 slot, so we sink the version into
     * the footer ring instead. */
    lv_obj_align(version, LV_ALIGN_TOP_MID, 0, 410 - 8);

    s_splash_screen = scr;
    lv_screen_load(s_splash_screen);
    (void)disp;
}

/* ===== Agent screen ================================================= */

/* Build a single ring (LVGL arc widget). The arc widget is a square
 * sized to 2*outer_radius and centred on the disc; the stroke width
 * equals (outer - inner). LVGL handles drawing the arc within the
 * widget's bounding box. Indicator angles run start_deg → start_deg +
 * sweep_deg (clockwise); the indicator value (0..VALUE_RANGE) drives
 * how far the indicator fills toward the end angle. */
static void build_ring(lv_obj_t *parent, int ring_idx, int outer_r, int inner_r)
{
    ui_ring_t *r = &s_rings[ring_idx];

    lv_obj_t *arc = lv_arc_create(parent);
    const int size = outer_r * 2;
    const int stroke = outer_r - inner_r;

    lv_obj_set_size(arc, size, size);
    lv_obj_align(arc, LV_ALIGN_TOP_LEFT,
                 AMOLED_CX - outer_r, AMOLED_CY - outer_r);
    lv_arc_set_range(arc, 0, AMOLED_ARC_VALUE_RANGE);
    lv_arc_set_bg_angles(arc, AMOLED_ARC_START_DEG,
                         AMOLED_ARC_START_DEG + AMOLED_ARC_SWEEP_DEG);
    lv_arc_set_value(arc, 0);

    /* Track (main) — dim grey, square caps; indicator — per-agent
     * colour, rounded caps. Border + padding off so the arc lives
     * exactly inside its bounding box. */
    lv_obj_set_style_arc_width(arc, stroke, LV_PART_MAIN);
    lv_obj_set_style_arc_color(arc, lv_color_hex(0x2F2F2F), LV_PART_MAIN);
    lv_obj_set_style_arc_rounded(arc, false, LV_PART_MAIN);

    lv_obj_set_style_arc_width(arc, stroke, LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(arc, true, LV_PART_INDICATOR);

    /* Hide the draggable knob and disable click events — render-only. */
    lv_obj_remove_style(arc, NULL, LV_PART_KNOB);
    lv_obj_remove_flag(arc, LV_OBJ_FLAG_CLICKABLE);

    r->arc = arc;
    r->pill_tag = NULL;
    r->pill_countdown = NULL;
}

/* Build a chip pill (R1/R2 type tag) + countdown label in the top gap. */
static void build_pill(lv_obj_t *parent, int ring_idx, int x)
{
    ui_ring_t *r = &s_rings[ring_idx];

    lv_obj_t *pill = lv_label_create(parent);
    lv_label_set_text(pill, "—");
    lv_obj_set_style_text_font(pill, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(pill, lv_color_hex(0xF9F2DF), 0);
    lv_obj_set_style_bg_color(pill, lv_color_hex(0x2E2E2E), 0);
    lv_obj_set_style_bg_opa(pill, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(pill, AMOLED_PILL_CHIP_RADIUS, 0);
    lv_obj_set_style_pad_hor(pill, AMOLED_PILL_PAD_H, 0);
    lv_obj_set_style_pad_ver(pill, AMOLED_PILL_PAD_V, 0);
    place_at(pill, x, AMOLED_PILL_Y, true);

    lv_obj_t *cd = lv_label_create(parent);
    lv_label_set_text(cd, "--");
    lv_obj_set_style_text_font(cd, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(cd, lv_color_hex(0xB0ACA0), 0);
    place_at(cd, x + AMOLED_PILL_CD_OFFSET_X, AMOLED_PILL_Y, true);

    r->pill_tag = pill;
    r->pill_countdown = cd;
}

static void build_agent_screen(void)
{
    lv_obj_t *scr = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);
    lv_obj_set_style_text_color(scr, lv_color_hex(0xF9F2DF), 0);
    lv_obj_set_style_pad_all(scr, 0, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    /* Three rings, outer → inner. Row 0 is the headline (heaviest
     * stroke), row 2 is the least-emphasised. */
    build_ring(scr, 0, AMOLED_R0_OUTER, AMOLED_R0_INNER);
    build_ring(scr, 1, AMOLED_R1_OUTER, AMOLED_R1_INNER);
    build_ring(scr, 2, AMOLED_R2_OUTER, AMOLED_R2_INNER);

    /* Header: flat "Usage" in the 90° top gap. */
    s_header_label = lv_label_create(scr);
    lv_label_set_text(s_header_label, "Usage");
    lv_obj_set_style_text_font(s_header_label, &lv_font_montserrat_28, 0);
    place_at(s_header_label, AMOLED_CX, AMOLED_HEADER_Y + 14, true);

    /* Central core: brand icon + row-0 percent + row-0 type tag. */
    s_core_icon = lv_image_create(scr);
    lv_image_set_src(s_core_icon, &icon_claude);
    place_at(s_core_icon, AMOLED_CX, AMOLED_CORE_ICON_Y, true);

    s_core_primary = lv_label_create(scr);
    lv_label_set_text(s_core_primary, "0%");
    /* Montserrat 48 is not bundled by default — fall back to 28 until
     * the font asset is added to the build. The HTML tuner previews
     * 48 px; verify glyph asset is present before flipping this. */
    lv_obj_set_style_text_font(s_core_primary, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(s_core_primary, lv_color_hex(0xF9F2DF), 0);
    place_at(s_core_primary, AMOLED_CX, AMOLED_CORE_PRIMARY_Y, true);

    s_core_secondary = lv_label_create(scr);
    lv_label_set_text(s_core_secondary, "");
    lv_obj_set_style_text_font(s_core_secondary, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(s_core_secondary, lv_color_hex(0xB0ACA0), 0);
    place_at(s_core_secondary, AMOLED_CX, AMOLED_CORE_SECONDARY_Y, true);

    /* Pills (R1 + R2) in the top gap. */
    build_pill(scr, 1, AMOLED_PILL_R1_X);
    build_pill(scr, 2, AMOLED_PILL_R2_X);

    /* Footer band — both labels at y=410, inside the safe disc. */
    s_footer_left = lv_label_create(scr);
    lv_obj_set_width(s_footer_left, AMOLED_FOOTER_LEFT_WIDTH);
    lv_label_set_long_mode(s_footer_left, LV_LABEL_LONG_DOT);
    lv_label_set_text(s_footer_left, "unpaired");
    lv_obj_set_style_text_font(s_footer_left, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(s_footer_left, lv_color_hex(0x5C5C5C), 0);
    lv_obj_align(s_footer_left, LV_ALIGN_TOP_LEFT,
                 AMOLED_FOOTER_LEFT_X, AMOLED_FOOTER_Y - 8);

    s_footer_right = lv_label_create(scr);
    lv_label_set_text(s_footer_right, "");
    lv_obj_set_style_text_font(s_footer_right, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(s_footer_right, lv_color_hex(0x5C5C5C), 0);
    lv_obj_set_style_text_align(s_footer_right, LV_TEXT_ALIGN_RIGHT, 0);
    /* Right-anchor: align to right side of footer, then shift left. */
    lv_obj_align(s_footer_right, LV_ALIGN_TOP_LEFT,
                 AMOLED_FOOTER_RIGHT_X - 160, AMOLED_FOOTER_Y - 8);
    lv_obj_set_width(s_footer_right, 160);

    s_agent_screen = scr;
}

/* Hide a ring + its pill. */
static void hide_ring(int idx)
{
    if (s_rings[idx].arc) lv_obj_add_flag(s_rings[idx].arc, LV_OBJ_FLAG_HIDDEN);
    if (s_rings[idx].pill_tag) lv_obj_add_flag(s_rings[idx].pill_tag, LV_OBJ_FLAG_HIDDEN);
    if (s_rings[idx].pill_countdown) lv_obj_add_flag(s_rings[idx].pill_countdown, LV_OBJ_FLAG_HIDDEN);
}
static void show_ring(int idx)
{
    if (s_rings[idx].arc) lv_obj_clear_flag(s_rings[idx].arc, LV_OBJ_FLAG_HIDDEN);
    if (s_rings[idx].pill_tag) lv_obj_clear_flag(s_rings[idx].pill_tag, LV_OBJ_FLAG_HIDDEN);
    if (s_rings[idx].pill_countdown) lv_obj_clear_flag(s_rings[idx].pill_countdown, LV_OBJ_FLAG_HIDDEN);
}

static void render_footer_locked(const agent_snapshot_t *snap)
{
    if (s_footer_left == NULL || s_footer_right == NULL) return;
    const char *cid = footer_cid_for(snap->agent);
    if (cid[0] == '\0') {
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
    /* Core icon + header. The header text is static "Usage" — agent
     * identity is conveyed by the core icon and ring palette swap. */
    lv_image_set_src(s_core_icon, agent_icon(snap->agent));
    lv_label_set_text(s_header_label, "Usage");

    render_footer_locked(snap);

    const agent_palette_t *pal = palette_for(snap->agent);

    int64_t now = (int64_t)time(NULL);
    const bool clock_synced = now > 1700000000;

    for (int i = 0; i < SNAPSHOT_MAX_SESSIONS; ++i) {
        ui_ring_t *r = &s_rings[i];
        if (r->arc == NULL) continue;

        if (i >= snap->session_count) {
            hide_ring(i);
            continue;
        }
        show_ring(i);

        const session_snapshot_t *s = &snap->sessions[i];
        float pct = s->used_pct;
        if (pct < 0.0f) pct = 0.0f;
        if (pct > 1.0f) pct = 1.0f;
        /* Post-reset auto-zero: once the wall clock crosses resets_at the
         * old window is logically gone. Keep the arc at 0 until the next
         * push delivers the new window's used_pct + resets_at. Mirrors
         * the cyd2usb behaviour exactly. */
        if (clock_synced && now >= s->resets_at) {
            pct = 0.0f;
        }

        /* Indicator value + colour. */
        lv_arc_set_value(r->arc, (int32_t)lroundf(pct * AMOLED_ARC_VALUE_RANGE));
        lv_obj_set_style_arc_color(r->arc, lv_color_hex(pal->rows[i]),
                                   LV_PART_INDICATOR);

        char cd_buf[16];
        if (clock_synced) {
            format_countdown(s->resets_at - now, cd_buf, sizeof(cd_buf));
        } else {
            snprintf(cd_buf, sizeof(cd_buf), "syncing...");
        }

        if (i == 0) {
            /* Row 0 → core: percent in the centre, type tag below. */
            char pct_buf[8];
            snprintf(pct_buf, sizeof(pct_buf), "%d%%", (int)lroundf(pct * 100.0f));
            lv_label_set_text(s_core_primary, pct_buf);
            lv_label_set_text(s_core_secondary, s->type);
        } else {
            /* Row 1 / 2 → top-gap pills with type + countdown. */
            if (r->pill_tag) lv_label_set_text(r->pill_tag, s->type);
            if (r->pill_countdown) lv_label_set_text(r->pill_countdown, cd_buf);
        }
    }

    /* If row 0 is the only visible row, clear the pills' text so a
     * stale countdown doesn't linger. show_ring already clears
     * hidden flags but does not touch text content. */
    if (snap->session_count <= 1) {
        if (s_rings[1].pill_tag) lv_label_set_text(s_rings[1].pill_tag, "");
        if (s_rings[1].pill_countdown) lv_label_set_text(s_rings[1].pill_countdown, "");
        if (s_rings[2].pill_tag) lv_label_set_text(s_rings[2].pill_tag, "");
        if (s_rings[2].pill_countdown) lv_label_set_text(s_rings[2].pill_countdown, "");
    }
}

static void show_agent_locked(const agent_snapshot_t *snap)
{
    refresh_footer_cid(snap->agent);
    render_snapshot_locked(snap);
    strncpy(s_visible_agent, snap->agent, sizeof(s_visible_agent) - 1);
    s_visible_agent[sizeof(s_visible_agent) - 1] = '\0';
    lv_screen_load(s_agent_screen);
}

/* Cycling helper — same shape as the cyd2usb profile. */
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

static void advance_to_next_agent_locked(void)
{
    cycle_buf_t buf = { .count = 0 };
    snapshot_store_foreach(cycle_collect, &buf);
    if (buf.count < 2) return;
    for (int i = 0; i < buf.count; ++i) {
        if (strcmp(buf.items[i].agent, s_visible_agent) == 0) {
            show_agent_locked(&buf.items[(i + 1) % buf.count]);
            return;
        }
    }
    show_agent_locked(&buf.items[0]);
}

static void tick_lvgl_cb(lv_timer_t *t)
{
    (void)t;
    if (s_visible_agent[0] == '\0') return;
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

/* ===== Public profile interface ===================================== */

void display_profile_init(void)
{
    lv_display_t *disp = amoled_co5300_driver_init();

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
    if (s_status_label == NULL) return;
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
    /* Only force a swap when still on the splash — subsequent pushes
     * refresh in place and let the 1 Hz tick handle cycling. */
    if (s_visible_agent[0] == '\0') {
        show_agent_locked(snap);
    }
    lvgl_port_unlock();
}
