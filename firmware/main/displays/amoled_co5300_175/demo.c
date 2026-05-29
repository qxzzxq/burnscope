/*
 * AMOLED CO5300 (Waveshare 1.75") driver-verification demo.
 *
 * Compiled in when `CONFIG_BURNSCOPE_AMOLED_DEMO=y` and called early
 * from `app_main` before the Wi-Fi / HTTP / NVS stack starts. Renders
 * a static test pattern that lets us verify, in one glance:
 *
 *   1. Panel comes up at all (anything visible == SLPOUT/DISPON worked,
 *      and the AXP2101 rail is on by default — if dark, the panel needs
 *      a PMIC rail-enable in driver.c).
 *   2. Pixel format (RGB565 byte order) — the R / G / B color bars
 *      must be red, green, blue, not some permutation.
 *   3. Pixel addressing (column / row offsets, the x=6 gap) — the corner
 *      markers must sit at the four corners of the visible disc, not
 *      shifted or wrapped.
 *   4. Center alignment — the yellow crosshair must land at (cx, cy).
 *   5. Brightness (0x51 register) — the white bar should look white.
 *
 * The function never returns the panel handle; once the screen is
 * loaded, `app_main` just sleeps forever (this is a one-shot test).
 */

#include "demo.h"

#include "driver.h"

#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lvgl.h"

static const char *TAG = "amoled_demo";

#define PANEL_W 466
#define PANEL_H 466
#define CX (PANEL_W / 2)
#define CY (PANEL_H / 2)

/* Helper: a filled rectangle with no border / padding / radius. */
static lv_obj_t *solid_rect(lv_obj_t *parent, int w, int h, uint32_t color)
{
    lv_obj_t *r = lv_obj_create(parent);
    lv_obj_set_size(r, w, h);
    lv_obj_set_style_bg_color(r, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(r, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(r, 0, 0);
    lv_obj_set_style_pad_all(r, 0, 0);
    lv_obj_set_style_radius(r, 0, 0);
    lv_obj_clear_flag(r, LV_OBJ_FLAG_SCROLLABLE);
    return r;
}

void amoled_demo_run(void)
{
    ESP_LOGI(TAG, "Initialising CO5300 panel for driver-verification demo");
    lv_display_t *disp = amoled_co5300_175_driver_init();
    if (disp == NULL) {
        ESP_LOGE(TAG, "Panel init returned NULL — driver bring-up failed.");
        return;
    }
    ESP_LOGI(TAG, "Panel handle acquired, building test pattern");

    if (!lvgl_port_lock(0)) {
        ESP_LOGE(TAG, "lvgl_port_lock failed");
        return;
    }

    lv_obj_t *scr = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);
    lv_obj_set_style_pad_all(scr, 0, 0);
    lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    /* Title at the top — verifies font rendering + that the top of
     * the framebuffer is actually visible (not offset off-screen). */
    lv_obj_t *title = lv_label_create(scr);
    lv_label_set_text(title, "AMOLED-1.75\nCO5300 test");
    lv_obj_set_style_text_color(title, lv_color_hex(0xF9F2DF), 0);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_24, 0);
    lv_obj_set_style_text_align(title, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 50);

    /* Four horizontal color bars across the middle band. R/G/B verify
     * the RGB565 byte order (swap_bytes / rgb_ele_order); the white bar
     * verifies the high end of each channel + brightness register. */
    struct { uint32_t hex; const char *name; int y; } bars[] = {
        { 0xFF0000, "R", 170 },
        { 0x00FF00, "G", 220 },
        { 0x0000FF, "B", 270 },
        { 0xFFFFFF, "W", 320 },
    };
    for (size_t i = 0; i < sizeof(bars) / sizeof(bars[0]); ++i) {
        lv_obj_t *bar = solid_rect(scr, 360, 40, bars[i].hex);
        lv_obj_align(bar, LV_ALIGN_TOP_MID, 0, bars[i].y);

        lv_obj_t *lbl = lv_label_create(scr);
        lv_label_set_text(lbl, bars[i].name);
        /* Black text on R/G/W bars, white on B. */
        uint32_t fg = (bars[i].hex == 0x0000FF) ? 0xFFFFFF : 0x000000;
        lv_obj_set_style_text_color(lbl, lv_color_hex(fg), 0);
        lv_obj_set_style_text_font(lbl, &lv_font_montserrat_28, 0);
        lv_obj_align_to(lbl, bar, LV_ALIGN_CENTER, 0, 0);
    }

    /* Centre crosshair (yellow) — verifies that (cx,cy) maps to the
     * actual physical centre of the disc and that the column / row
     * offsets (the x=6 gap) are correct. */
    lv_obj_t *vline = solid_rect(scr, 2, 40, 0xFFFF00);
    lv_obj_align(vline, LV_ALIGN_TOP_LEFT, CX - 1, CY - 20);
    lv_obj_t *hline = solid_rect(scr, 40, 2, 0xFFFF00);
    lv_obj_align(hline, LV_ALIGN_TOP_LEFT, CX - 20, CY - 1);

    /* Corner markers: 16x16 squares at the four corners of the visible
     * disc, just inside the safe-radius. If any corner is missing or
     * clipped on one edge, the address-window offsets are off. */
    struct { int x, y; uint32_t hex; } corners[] = {
        {  68,  68, 0xFFFF00 },   /* top-left,    yellow  */
        { 382,  68, 0xFF00FF },   /* top-right,   magenta */
        {  68, 382, 0x00FFFF },   /* bottom-left, cyan    */
        { 382, 382, 0xFFFFFF },   /* bottom-right, white  */
    };
    for (size_t i = 0; i < sizeof(corners) / sizeof(corners[0]); ++i) {
        lv_obj_t *sq = solid_rect(scr, 16, 16, corners[i].hex);
        lv_obj_align(sq, LV_ALIGN_TOP_LEFT, corners[i].x - 8, corners[i].y - 8);
    }

    lv_screen_load(scr);
    lvgl_port_unlock();

    ESP_LOGI(TAG, "Test pattern loaded — leaving the panel on; app_main "
                  "will sleep forever from here.");
}
