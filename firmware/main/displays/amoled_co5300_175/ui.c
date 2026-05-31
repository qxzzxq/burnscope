/*
 * amoled_co5300_175 LVGL UI — concentric-arc dial on a 466×466 round
 * AMOLED (Waveshare 1.75" / CO5300). Clone of the amoled_sh8601 (1.43")
 * UI: same 466×466 geometry, same layout constants. Differences are
 * confined to display_profile_init — this board has no touch indev and
 * no IMU-driven orientation watcher.
 *
 * Two screens:
 *   - Splash: title + status line + version footer. Used during boot,
 *     provisioning, and "waiting for daemon".
 *   - Agent: two concentric rings (row 0 outer, row 1 inner) around a
 *     central core that stacks the agent brand icon, the row-0 type
 *     chip + percentage + reset countdown, and the row-1 type chip +
 *     percentage + reset countdown. Footer at the bottom of the disc
 *     is two stacked centred lines: "updated N ago" above the
 *     client_id.
 *
 * Layout constants below are pasted from the HTML tuner at
 * `firmware/scripts/amoled-preview.html`. The HTML is the authoritative
 * visual contract.
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

#include "esp_err.h"
#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "lvgl.h"
#include "nvs.h"

#include "axp2101.h"
#include "burn_idle_adapter.h"
#include "driver.h"
#include "nvs_store.h"
#include "orientation.h"
#include "snapshot.h"
#include "touch.h"
#include "version.h"

/* Per-agent brand icons. 70×70 ARGB8888 assets dedicated to the AMOLED
 * profile; the 24×24 variants (`icon_claude`, `icon_codex`) remain in
 * use by the cyd2usb profile. */
LV_IMAGE_DECLARE(icon_claude_70);
LV_IMAGE_DECLARE(icon_codex_70);

static const char *TAG = "render";

/* ===== Layout constants — paste from amoled-preview.html ============= */
#define AMOLED_CX                      233
#define AMOLED_CY                      233
#define AMOLED_SAFE_RADIUS             220

#define AMOLED_R0_OUTER                215
#define AMOLED_R0_INNER                190   /* stroke 25 */
#define AMOLED_R1_OUTER                184
#define AMOLED_R1_INNER                164   /* stroke 20 */
/* R2 removed — preview is a 2-ring design. SNAPSHOT_MAX_SESSIONS in
 * snapshot.h is unchanged; we simply ignore any third slot on AMOLED. */

#define AMOLED_ARC_START_DEG           135
#define AMOLED_ARC_SWEEP_DEG           270
#define AMOLED_ARC_VALUE_RANGE         1000

#define AMOLED_CORE_ICON_Y             130   /* icon centre */
#define AMOLED_CORE_CHIP_PRIMARY_Y     195   /* chip centre */
#define AMOLED_CORE_PRIMARY_Y          235   /* primary % centre */
#define AMOLED_CORE_CHIP_SECONDARY_Y   285
#define AMOLED_CORE_SECONDARY_Y        315
#define AMOLED_CORE_CHIP_RADIUS        4
#define AMOLED_CORE_CHIP_PAD_H         5
#define AMOLED_CORE_CHIP_PAD_V         3
#define AMOLED_CORE_COUNTDOWN_GAP      10    /* gap between % and countdown */

/* Battery indicator centre — in the clear band between the secondary %
 * (AMOLED_CORE_SECONDARY_Y) and the footer (AMOLED_FOOTER_UPDATED_Y). */
#define AMOLED_BATTERY_Y               367
#define AMOLED_FOOTER_UPDATED_Y        410   /* top footer line centre */
#define AMOLED_FOOTER_EMAIL_Y          430   /* bottom footer line centre */
#define AMOLED_FOOTER_WIDTH            420   /* ellipsis clamp width */

#define AMOLED_NUM_RINGS               2

/* ===== Splash widgets =============================================== */
static lv_obj_t *s_splash_screen = NULL;
static lv_obj_t *s_status_label  = NULL;

/* ===== Agent-screen widgets (built once, mutated per snapshot) ====== */
static lv_obj_t *s_agent_screen        = NULL;
static lv_obj_t *s_core_icon           = NULL;
static lv_obj_t *s_chip_primary        = NULL;  /* row-0 type pill   */
static lv_obj_t *s_core_primary        = NULL;  /* M48 percentage    */
static lv_obj_t *s_core_primary_cd     = NULL;  /* M14 countdown     */
static lv_obj_t *s_chip_secondary      = NULL;
static lv_obj_t *s_core_secondary      = NULL;  /* M36 percentage    */
static lv_obj_t *s_core_secondary_cd   = NULL;
static lv_obj_t *s_footer_updated      = NULL;  /* "updated N ago"   */
static lv_obj_t *s_footer_email        = NULL;  /* client_id         */
static lv_obj_t *s_battery_label       = NULL;  /* AXP2101 % + glyph, above footer */

/* Battery telemetry. `s_battery_ok` is the one-shot result of axp2101_init
 * (false when no AXP2101 / no PMU board); `s_battery` is the last good
 * reading and `s_battery_valid` gates whether we have one yet. The I2C
 * sample cadence (10 s, in the tick) is decoupled from the paint
 * (apply_battery_locked, called on agent swap + after each sample) so a
 * 5 s cycle swap doesn't trigger an I2C read. */
static bool             s_battery_ok    = false;
static bool             s_battery_valid = false;
static axp2101_status_t s_battery       = { 0 };

typedef struct {
    lv_obj_t *arc;
} ui_ring_t;
static ui_ring_t s_rings[AMOLED_NUM_RINGS];

/* The agent we are currently rendering ("" when on splash). */
static char s_visible_agent[SNAPSHOT_AGENT_MAX] = "";

/* Cached client-id per known agent. Two refresh boundaries:
 *  (a) on agent swap (show_agent_locked → refresh_footer_cid), and
 *  (b) every 10 s from the 1 Hz tick — covers the factory-reset path
 *      in factory_reset.c that wipes NVS but does not reboot (the
 *      narrow cred-erase-fails-but-pair-erase-succeeds branch). The
 *      cyd2usb profile is structurally identical to (a) only; this
 *      AMOLED variant deliberately adds (b) because the on-display
 *      email persisting after a factory-reset is more visually
 *      jarring on the 466×466 panel.
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
    char buf[BURNSCOPE_CLIENT_ID_MAX];
    esp_err_t err = nvs_store_load_client_id(agent, buf, sizeof(buf));
    if (err == ESP_OK) {
        memcpy(s_footer_cid[idx], buf, sizeof(s_footer_cid[idx]));
        return;
    }
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        /* Slot empty (factory-reset or never bound). */
        s_footer_cid[idx][0] = '\0';
        return;
    }
    /* Real NVS error (corruption, invalid args, …) — keep whatever
     * value we already cached rather than blanking, so a transient
     * read failure doesn't wipe a valid on-screen email. */
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

/* Per-agent ring palette (FR-4.7). Two rings only; track the cyd2usb
 * palette so a multi-screen setup stays visually unified. */
typedef struct {
    const char *agent;
    uint32_t rows[AMOLED_NUM_RINGS];
} agent_palette_t;

static const agent_palette_t AGENT_PALETTES[] = {
    { "claude", { 0xDE7356, 0xA4A049 } },
    { "codex",  { 0x81C3DD, 0xA4A049 } },
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
    if (strcmp(agent, "codex") == 0) return &icon_codex_70;
    return &icon_claude_70;
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
 * timezone-free justification). Writes "" if either timestamp is
 * pre-unix-2023 so the unsynced state surfaces as an empty line. */
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
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 180 - 14);

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
    /* y=410 — same band as the agent-screen footer. */
    lv_obj_align(version, LV_ALIGN_TOP_MID, 0, 410 - 8);

    s_splash_screen = scr;
    lv_screen_load(s_splash_screen);
    (void)disp;
}

/* ===== Agent screen ================================================= */

/* Build a single ring (LVGL arc widget). Mirrors the cyd2usb pattern.
 * The arc widget is a square sized to 2*outer_radius and centred on
 * the disc; the stroke width equals (outer - inner). LVGL handles
 * drawing the arc within the widget's bounding box. */
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

    /* Track (main) — dim grey, rounded caps; indicator — per-agent
     * colour, rounded caps. Border + padding off so the arc lives
     * exactly inside its bounding box. */
    lv_obj_set_style_arc_width(arc, stroke, LV_PART_MAIN);
    lv_obj_set_style_arc_color(arc, lv_color_hex(0x2F2F2F), LV_PART_MAIN);
    lv_obj_set_style_arc_rounded(arc, true, LV_PART_MAIN);

    lv_obj_set_style_arc_width(arc, stroke, LV_PART_INDICATOR);
    lv_obj_set_style_arc_rounded(arc, true, LV_PART_INDICATOR);

    /* Hide the draggable knob and disable click events — render-only. */
    lv_obj_remove_style(arc, NULL, LV_PART_KNOB);
    lv_obj_remove_flag(arc, LV_OBJ_FLAG_CLICKABLE);

    r->arc = arc;
}

/* Build a chip pill — a label with a dark rounded background, sized to
 * its text content, centred horizontally at `center_y`. */
static lv_obj_t *build_chip(lv_obj_t *parent, int center_y)
{
    lv_obj_t *chip = lv_label_create(parent);
    lv_label_set_text(chip, "");
    lv_obj_set_style_text_font(chip, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(chip, lv_color_hex(0xF9F2DF), 0);
    lv_obj_set_style_bg_color(chip, lv_color_hex(0x2E2E2E), 0);
    lv_obj_set_style_bg_opa(chip, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(chip, AMOLED_CORE_CHIP_RADIUS, 0);
    lv_obj_set_style_pad_hor(chip, AMOLED_CORE_CHIP_PAD_H, 0);
    lv_obj_set_style_pad_ver(chip, AMOLED_CORE_CHIP_PAD_V, 0);
    /* Initial align — will be re-centred each render against the
     * label's current width. */
    lv_obj_align(chip, LV_ALIGN_TOP_MID, 0, center_y);
    return chip;
}

/* Create the % label and its countdown sibling as direct children of
 * `parent`. They are positioned independently so the percentage can
 * be centred on the disc while the countdown floats off to the right
 * — centring the *pair* together would pull the % off-axis as the
 * countdown width changes. */
static void build_pct_pair(lv_obj_t *parent, const lv_font_t *pct_font,
                           lv_obj_t **out_pct, lv_obj_t **out_cd)
{
    lv_obj_t *pct = lv_label_create(parent);
    lv_label_set_text(pct, "0%");
    lv_obj_set_style_text_font(pct, pct_font, 0);
    lv_obj_set_style_text_color(pct, lv_color_hex(0xF9F2DF), 0);

    lv_obj_t *cd = lv_label_create(parent);
    lv_label_set_text(cd, "");
    lv_obj_set_style_text_font(cd, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(cd, lv_color_hex(0x5C5C5C), 0);

    *out_pct = pct;
    *out_cd  = cd;
}

/* Centre an object's mid-point on (AMOLED_CX, center_y).
 * Uses LV_ALIGN_TOP_LEFT so the (x, y) offset is absolute relative
 * to the screen — required because any prior align (e.g. TOP_MID)
 * would otherwise stack on top of the offset and pull the object
 * off-centre. Call after any text change so the object stays
 * centred as its width changes ("1%" → "100%"). */
static void recenter_at(lv_obj_t *obj, int center_y)
{
    lv_obj_update_layout(obj);
    int w = lv_obj_get_width(obj);
    int h = lv_obj_get_height(obj);
    lv_obj_align(obj, LV_ALIGN_TOP_LEFT,
                 AMOLED_CX - w / 2, center_y - h / 2);
}

static void build_agent_screen(void)
{
    lv_obj_t *scr = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);
    lv_obj_set_style_text_color(scr, lv_color_hex(0xF9F2DF), 0);
    lv_obj_set_style_pad_all(scr, 0, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    /* Two rings, outer → inner. Row 0 is the headline (heaviest
     * stroke), row 1 the secondary. */
    build_ring(scr, 0, AMOLED_R0_OUTER, AMOLED_R0_INNER);
    build_ring(scr, 1, AMOLED_R1_OUTER, AMOLED_R1_INNER);

    /* Brand icon, 70×70, centred at (cx, icon_y). */
    s_core_icon = lv_image_create(scr);
    lv_image_set_src(s_core_icon, &icon_claude_70);
    lv_obj_align(s_core_icon, LV_ALIGN_TOP_LEFT,
                 AMOLED_CX - 35, AMOLED_CORE_ICON_Y - 35);

    /* Primary stack: chip + percentage + countdown. */
    s_chip_primary = build_chip(scr, AMOLED_CORE_CHIP_PRIMARY_Y);
    build_pct_pair(scr, &lv_font_montserrat_48,
                   &s_core_primary, &s_core_primary_cd);

    /* Secondary stack: chip + percentage + countdown. */
    s_chip_secondary = build_chip(scr, AMOLED_CORE_CHIP_SECONDARY_Y);
    build_pct_pair(scr, &lv_font_montserrat_36,
                   &s_core_secondary, &s_core_secondary_cd);

    /* Footer band — two centred lines stacked at the bottom of the
     * disc. M14 #5C5C5C, both full-width so centring is exact. */
    s_footer_updated = lv_label_create(scr);
    lv_label_set_text(s_footer_updated, "");
    lv_obj_set_style_text_font(s_footer_updated, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(s_footer_updated, lv_color_hex(0x5C5C5C), 0);
    lv_obj_set_style_text_align(s_footer_updated, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_width(s_footer_updated, AMOLED_FOOTER_WIDTH);
    lv_label_set_long_mode(s_footer_updated, LV_LABEL_LONG_DOT);
    lv_obj_align(s_footer_updated, LV_ALIGN_TOP_MID, 0,
                 AMOLED_FOOTER_UPDATED_Y - 8);

    s_footer_email = lv_label_create(scr);
    lv_label_set_text(s_footer_email, "unpaired");
    lv_obj_set_style_text_font(s_footer_email, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(s_footer_email, lv_color_hex(0x5C5C5C), 0);
    lv_obj_set_style_text_align(s_footer_email, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_width(s_footer_email, AMOLED_FOOTER_WIDTH);
    lv_label_set_long_mode(s_footer_email, LV_LABEL_LONG_DOT);
    lv_obj_align(s_footer_email, LV_ALIGN_TOP_MID, 0,
                 AMOLED_FOOTER_EMAIL_Y - 8);

    /* Battery indicator — a battery/charge glyph + percentage centred in
     * the clear band between the secondary % and the footer. Hidden until
     * a reading lands (and stays hidden on boards with no battery, see
     * apply_battery_locked). M16 to match the chips/footer. */
    s_battery_label = lv_label_create(scr);
    lv_label_set_text(s_battery_label, "");
    lv_obj_set_style_text_font(s_battery_label, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(s_battery_label, lv_color_hex(0x8A8A8A), 0);
    lv_obj_add_flag(s_battery_label, LV_OBJ_FLAG_HIDDEN);
    lv_obj_align(s_battery_label, LV_ALIGN_TOP_MID, 0, AMOLED_BATTERY_Y - 8);

    s_agent_screen = scr;
}

static void show_obj(lv_obj_t *o, bool visible)
{
    if (o == NULL) return;
    if (visible) lv_obj_clear_flag(o, LV_OBJ_FLAG_HIDDEN);
    else         lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
}

static void render_footer_locked(const agent_snapshot_t *snap)
{
    if (s_footer_updated == NULL || s_footer_email == NULL) return;
    const char *cid = footer_cid_for(snap->agent);
    if (cid[0] == '\0') {
        /* Unpaired: top empty, bottom = "unpaired". */
        lv_label_set_text(s_footer_updated, "");
        lv_label_set_text(s_footer_email, "unpaired");
        return;
    }
    lv_label_set_text(s_footer_email, cid);

    char buf[32];
    format_updated_relative(snap->captured_at, buf, sizeof(buf));
    /* format_updated_relative writes "" when the wall clock is unsynced —
     * that already gives us the "top empty" state. */
    lv_label_set_text(s_footer_updated, buf);
}

/* Read a fresh AXP2101 snapshot into the cache. No-op — leaving the
 * previous reading intact — when the PMU is absent or the I2C read fails,
 * so a transient bus hiccup doesn't blank the indicator. Does its own
 * I2C, so callers should throttle it (see the 10 s tick block). */
static void sample_battery(void)
{
    if (!s_battery_ok) return;
    axp2101_status_t st;
    if (axp2101_read(&st)) {
        s_battery = st;
        s_battery_valid = true;
    }
}

/* Paint the battery label from the cached reading. Hidden when the PMU is
 * absent, no reading has landed yet, or no pack is connected (USB-only
 * power). Picks a charge/battery glyph by level and tints a low,
 * not-charging pack red. Caller must hold lvgl_port_lock. */
static void apply_battery_locked(void)
{
    if (s_battery_label == NULL) return;
    if (!s_battery_ok || !s_battery_valid || !s_battery.present) {
        lv_obj_add_flag(s_battery_label, LV_OBJ_FLAG_HIDDEN);
        return;
    }

    int pct = s_battery.percent;
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;

    const char *glyph;
    if (s_battery.charging) glyph = LV_SYMBOL_CHARGE;
    else if (pct >= 88)     glyph = LV_SYMBOL_BATTERY_FULL;
    else if (pct >= 63)     glyph = LV_SYMBOL_BATTERY_3;
    else if (pct >= 38)     glyph = LV_SYMBOL_BATTERY_2;
    else if (pct >= 13)     glyph = LV_SYMBOL_BATTERY_1;
    else                    glyph = LV_SYMBOL_BATTERY_EMPTY;

    uint32_t color;
    if (s_battery.charging) color = 0x7FB069;  /* charging — soft green   */
    else if (pct < 15)      color = 0xE05A4F;  /* low, uncharging — red    */
    else                    color = 0x8A8A8A;  /* normal — dim grey        */
    lv_obj_set_style_text_color(s_battery_label, lv_color_hex(color), 0);

    char buf[16];
    snprintf(buf, sizeof(buf), "%s %d%%", glyph, pct);
    lv_label_set_text(s_battery_label, buf);
    lv_obj_clear_flag(s_battery_label, LV_OBJ_FLAG_HIDDEN);
    recenter_at(s_battery_label, AMOLED_BATTERY_Y);
}

/* Render one (chip, %-label, countdown-label) stack from a
 * session_snapshot. The chip and percentage are centred on the disc
 * axis; the countdown floats to the right of the percentage and its
 * baseline is aligned with the percentage's. Hides the whole stack
 * if `visible` is false. */
static void render_stack_locked(bool visible,
                                lv_obj_t *chip, int chip_y,
                                lv_obj_t *pct_label, int pct_y,
                                lv_obj_t *cd_label,
                                const session_snapshot_t *s,
                                bool clock_synced, int64_t now)
{
    show_obj(chip, visible);
    show_obj(pct_label, visible);
    show_obj(cd_label, visible);
    if (!visible) return;

    /* Synthesise the reset time locally for rolling windows at idle
     * (codex). For fixed-window agents (claude) this is a no-op. See
     * effective_resets_at() in snapshot.h. */
    int64_t reset_at = effective_resets_at(s, now);
    float pct = s->used_pct;
    if (pct < 0.0f) pct = 0.0f;
    if (pct > 1.0f) pct = 1.0f;
    if (clock_synced && now >= reset_at) {
        pct = 0.0f;
    }

    char pct_buf[8];
    snprintf(pct_buf, sizeof(pct_buf), "%d%%", (int)lroundf(pct * 100.0f));

    char cd_buf[16];
    if (clock_synced) {
        format_countdown(reset_at - now, cd_buf, sizeof(cd_buf));
    } else {
        snprintf(cd_buf, sizeof(cd_buf), "syncing...");
    }

    lv_label_set_text(chip, s->type);
    lv_label_set_text(pct_label, pct_buf);
    lv_label_set_text(cd_label, cd_buf);

    /* Centre the chip and the percentage on the disc axis. */
    recenter_at(chip, chip_y);
    recenter_at(pct_label, pct_y);

    /* Position the countdown to the right of the centred percentage,
     * with its bottom edge aligned to the percentage's bottom edge so
     * the baselines visually line up. */
    lv_obj_update_layout(pct_label);
    lv_obj_update_layout(cd_label);
    int pct_w = lv_obj_get_width(pct_label);
    int pct_h = lv_obj_get_height(pct_label);
    int cd_h  = lv_obj_get_height(cd_label);
    lv_obj_align(cd_label, LV_ALIGN_TOP_LEFT,
                 AMOLED_CX + pct_w / 2 + AMOLED_CORE_COUNTDOWN_GAP,
                 pct_y + pct_h / 2 - cd_h);
}

static void render_snapshot_locked(const agent_snapshot_t *snap)
{
    /* Brand icon and palette swap. */
    lv_image_set_src(s_core_icon, agent_icon(snap->agent));

    render_footer_locked(snap);

    const agent_palette_t *pal = palette_for(snap->agent);

    int64_t now = (int64_t)time(NULL);
    const bool clock_synced = now > 1700000000;

    /* Per-ring arc update. */
    for (int i = 0; i < AMOLED_NUM_RINGS; ++i) {
        ui_ring_t *r = &s_rings[i];
        if (r->arc == NULL) continue;

        if (i >= snap->session_count) {
            lv_obj_add_flag(r->arc, LV_OBJ_FLAG_HIDDEN);
            continue;
        }
        lv_obj_clear_flag(r->arc, LV_OBJ_FLAG_HIDDEN);

        const session_snapshot_t *s = &snap->sessions[i];
        int64_t reset_at = effective_resets_at(s, now);
        float pct = s->used_pct;
        if (pct < 0.0f) pct = 0.0f;
        if (pct > 1.0f) pct = 1.0f;
        if (clock_synced && now >= reset_at) {
            pct = 0.0f;
        }
        lv_arc_set_value(r->arc, (int32_t)lroundf(pct * AMOLED_ARC_VALUE_RANGE));
        lv_obj_set_style_arc_color(r->arc, lv_color_hex(pal->rows[i]),
                                   LV_PART_INDICATOR);
    }

    /* Core stacks. The primary always shows (defensive against an
     * impossible session_count==0); the secondary follows session_count. */
    render_stack_locked(snap->session_count >= 1,
                        s_chip_primary,   AMOLED_CORE_CHIP_PRIMARY_Y,
                        s_core_primary,   AMOLED_CORE_PRIMARY_Y,
                        s_core_primary_cd,
                        &snap->sessions[0], clock_synced, now);
    render_stack_locked(snap->session_count >= 2,
                        s_chip_secondary, AMOLED_CORE_CHIP_SECONDARY_Y,
                        s_core_secondary, AMOLED_CORE_SECONDARY_Y,
                        s_core_secondary_cd,
                        &snap->sessions[1], clock_synced, now);
}

static void show_agent_locked(const agent_snapshot_t *snap)
{
    refresh_footer_cid(snap->agent);
    render_snapshot_locked(snap);
    apply_battery_locked();
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
    /* Periodically re-read the footer CID from NVS so a factory-reset
     * (which wipes NVS behind our back) is reflected within 10 seconds
     * instead of persisting the previous owner's email indefinitely.
     * Delegates to refresh_footer_cid so the return-code handling lives
     * in one place — see its definition near the cache declaration. */
    {
        static int footer_refresh_ticks = 0;
        if (++footer_refresh_ticks >= 10) {
            footer_refresh_ticks = 0;
            for (size_t i = 0; i < FOOTER_AGENT_COUNT; ++i) {
                refresh_footer_cid(FOOTER_AGENTS[i]);
            }
            /* Battery moves slowly — resample on the same 10 s cadence as
             * the footer, then repaint the indicator from the cache. */
            sample_battery();
            apply_battery_locked();
        }
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

/* LVGL touch indev callback. Polls the CST9217 every LVGL tick and, on a
 * fresh-press (released → pressed) transition, notifies the burn-in
 * adapter so the panel wakes from any idle state. The LVGL coordinates
 * fed in let any future touch-aware UI work transparently — the burn-in
 * wake is the same code path that drives UI input. Mirrors the 1.43"
 * profile's indev. */
static void touch_indev_read_cb(lv_indev_t *indev, lv_indev_data_t *data)
{
    (void)indev;
    static bool prev_pressed = false;
    uint16_t x = 0;
    uint16_t y = 0;
    const bool pressed = touch_read(&x, &y);
    if (pressed && !prev_pressed) {
        burn_idle_adapter_notify_touch();
    }
    prev_pressed = pressed;
    if (pressed) {
        data->state   = LV_INDEV_STATE_PRESSED;
        data->point.x = x;
        data->point.y = y;
    } else {
        data->state = LV_INDEV_STATE_RELEASED;
    }
}

void display_profile_init(void)
{
    /* Idempotency guard. The header documents this function as safe to
     * call once at boot; without the guard a second call would create a
     * second 1 Hz lv_timer and the agent-cycling state machine would
     * advance twice as fast. */
    static bool s_initialized = false;
    if (s_initialized) return;

    lv_display_t *disp = amoled_co5300_175_driver_init();
    touch_init();
    /* AXP2101 battery telemetry shares the touch/IMU I2C0 bus. Probe it
     * here and take one reading so the indicator is warm before the agent
     * screen first shows; false (no PMU / no board support) leaves the
     * indicator permanently hidden. */
    s_battery_ok = axp2101_init();
    sample_battery();

    if (!lvgl_port_lock(0)) {
        ESP_LOGE(TAG, "lvgl_port_lock failed during init");
        return;
    }
    build_splash(disp);
    build_agent_screen();
    lv_timer_create(tick_lvgl_cb, 1000, NULL);

    /* CST9217 touch indev — drives both any future touch UI and the
     * burn-in touch-wake source (notify_touch in touch_indev_read_cb). */
    lv_indev_t *touch_indev = lv_indev_create();
    lv_indev_set_type(touch_indev, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(touch_indev, touch_indev_read_cb);
    lv_indev_set_display(touch_indev, disp);

    lvgl_port_unlock();

    /* Wake sources: touch (CST9217 indev above), the GPIO0 button, the
     * QMI8658 motion sampler, and incoming /summary pushes (the latter
     * three in burn_idle_adapter). */
    burn_idle_adapter_start();
    /* Orientation watcher runs after the burn-in adapter so its
     * qmi8658_init has already brought up the IMU; the watcher's task
     * short-circuits gracefully if the chip was missing or init failed. */
    orientation_start(disp);
    s_initialized = true;
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
