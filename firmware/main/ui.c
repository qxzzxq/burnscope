/*
 * Phase 1 splash screen. Replaced by the full UI in Phase 2.
 */

#include "ui.h"

#include "esp_log.h"
#include "esp_lvgl_port.h"

#include "version.h"

static const char *TAG = "render";

static lv_obj_t *s_status_label = NULL;

void ui_init(lv_display_t *disp)
{
    if (!lvgl_port_lock(0)) {
        ESP_LOGE(TAG, "lvgl_port_lock failed during ui_init");
        return;
    }

    lv_obj_t *scr = lv_display_get_screen_active(disp);
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);
    lv_obj_set_style_text_color(scr, lv_color_white(), 0);

    lv_obj_t *title = lv_label_create(scr);
    lv_label_set_text(title, "BurnScope");
    lv_obj_set_style_text_font(title, &lv_font_montserrat_24, 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 16);

    s_status_label = lv_label_create(scr);
    lv_label_set_text(s_status_label, "Booting...");
    lv_obj_set_style_text_font(s_status_label, &lv_font_montserrat_24, 0);
    lv_obj_center(s_status_label);

    lv_obj_t *version = lv_label_create(scr);
    lv_label_set_text(version, "v" BURNSCOPE_FW_VERSION);
    lv_obj_set_style_text_color(version, lv_color_hex(0x808080), 0);
    lv_obj_align(version, LV_ALIGN_BOTTOM_LEFT, 8, -6);

    lvgl_port_unlock();
}

void ui_set_status(const char *text)
{
    if (s_status_label == NULL) {
        return;
    }
    if (!lvgl_port_lock(0)) {
        ESP_LOGE(TAG, "lvgl_port_lock failed in ui_set_status");
        return;
    }
    lv_label_set_text(s_status_label, text != NULL ? text : "");
    lvgl_port_unlock();
}
