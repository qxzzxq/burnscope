/*
 * Waveshare ESP32-S3-Touch-AMOLED-1.75 bring-up (CO5300, 466×466 round).
 *
 * Clone of the amoled_sh8601 (1.43") profile. Differences:
 *   - CO5300-only: no RDID1 probe / SH8601 fallback. This board is
 *     always a CO5300, so we drop the runtime detection (and the
 *     read_lcd_id.c / nvs_store LCD-id cache it depended on) and ship a
 *     single init table.
 *   - Different GPIO pins (see #define block; from the Waveshare 1.75
 *     BSP `esp32_s3_touch_amoled_1_75.h`).
 *   - Init table mirrored from the vendor BSP's CO5300 sequence
 *     (`esp32_s3_touch_amoled_1_75.c`), tuned to ramp brightness to the
 *     burn-in default rather than the vendor's 100 %.
 *
 * Still uses the managed component `espressif/esp_lcd_sh8601`: it frames
 * the QSPI command phase (cmd=0x02 + 24-bit address carrying the real
 * command) that this AMOLED family — SH8601 and CO5300 alike — expects.
 * The 1.43" profile already drives CO5300 silicon through it.
 *
 * The board carries a QMI8658 IMU (see qmi8658.c / orientation.c) for
 * auto-rotate + motion-wake. Touch (CST9217) and the AXP2101 PMIC are
 * out of scope for this profile pass; RST is a direct GPIO (39) and the
 * panel rail is on by the board's power-on defaults (verified — first
 * light works with no PMIC code).
 */

#include "driver.h"

#include "esp_err.h"
#include "esp_log.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_sh8601.h"
#include "esp_lvgl_port.h"

static const char *TAG = "amoled175_drv";

/* QSPI + panel pins — Waveshare 1.75" BSP (esp32_s3_touch_amoled_1_75.h). */
#define LCD_HOST            SPI2_HOST
#define PIN_NUM_LCD_SCLK    38   /* BSP_LCD_PCLK  */
#define PIN_NUM_LCD_D0      4    /* BSP_LCD_DATA0 */
#define PIN_NUM_LCD_D1      5    /* BSP_LCD_DATA1 */
#define PIN_NUM_LCD_D2      6    /* BSP_LCD_DATA2 */
#define PIN_NUM_LCD_D3      7    /* BSP_LCD_DATA3 */
#define PIN_NUM_LCD_CS      12   /* BSP_LCD_CS    */
#define PIN_NUM_LCD_RST     39   /* BSP_LCD_RST   */

#define LCD_H_RES           466
#define LCD_V_RES           466
#define LCD_BIT_PER_PIXEL   16
/* 80 MHz is the QSPI clock Waveshare uses on this panel family. */
#define LCD_PIXEL_CLOCK_HZ  (80 * 1000 * 1000)

/* Brightness ramp end value. 0xB2 = 178/255 ≈ 70 %, matching the burn-in
 * FSD's DEFAULT_BRIGHTNESS and the BURNSCOPE_AMOLED_ACTIVE_BRIGHTNESS_PCT
 * default. The vendor BSP ramps to 0xFF (100 %); we cap lower so the
 * panel isn't burned at full brightness before the idle adapter takes
 * over. */
#define CO5300_DEFAULT_BRIGHTNESS  0xB2

/* CO5300 init sequence mirrored from the Waveshare 1.75" BSP
 * (`esp32_s3_touch_amoled_1_75.c` lcd_init_cmds), with these deviations:
 *   - The 0x2A/0x2B column/row address window is dropped: the
 *     esp_lcd_sh8601 component programs CASET/RASET itself on each flush
 *     from the panel gap (set below), so a fixed window here would just
 *     be overwritten.
 *   - The 0x3A pixel-format command is dropped: the component sets it
 *     from `bits_per_pixel` (16 → RGB565), and warns if the init table
 *     also carries one. The 1.43" CO5300 table omits it for the same
 *     reason; the vendor BSP includes it because it drives the panel via
 *     the separate esp_lcd_co5300 component, which doesn't auto-set it.
 *   - Brightness ramps 0x00 → CO5300_DEFAULT_BRIGHTNESS around DISPON
 *     (instead of a single 0xFF) to suppress the first-frame junk flash
 *     and avoid full-brightness burn-in.
 */
static const sh8601_lcd_init_cmd_t s_co5300_init_cmds[] = {
    { 0xFE, (uint8_t[]){ 0x20 }, 1, 0 },   /* page select 0x20            */
    { 0x19, (uint8_t[]){ 0x10 }, 1, 0 },
    { 0x1C, (uint8_t[]){ 0xA0 }, 1, 0 },
    { 0xFE, (uint8_t[]){ 0x00 }, 1, 0 },   /* page select 0x00 (user)     */
    { 0xC4, (uint8_t[]){ 0x80 }, 1, 0 },   /* SPIMODECTL: stay in QSPI    */
    { 0x35, (uint8_t[]){ 0x00 }, 1, 0 },   /* TE on (line unused, benign) */
    { 0x53, (uint8_t[]){ 0x20 }, 1, 0 },   /* WCTRLD1: brightness ctl on  */
    { 0x63, (uint8_t[]){ 0xFF }, 1, 0 },   /* HBM ceiling max             */
    { 0x51, (uint8_t[]){ 0x00 }, 1, 0 },   /* brightness 0 before DISPON  */
    { 0x11, NULL, 0, 600 },                /* SLPOUT, 600ms settle        */
    { 0x29, NULL, 0, 10 },                 /* DISPON                      */
    { 0x51, (uint8_t[]){ CO5300_DEFAULT_BRIGHTNESS }, 1, 0 }, /* ramp ~70%*/
};

static lv_display_t              *s_display      = NULL;
static esp_lcd_panel_handle_t     s_panel_handle = NULL;
static esp_lcd_panel_io_handle_t  s_io_handle    = NULL;

/* CO5300/SH8601 QSPI command framing: the managed component wraps each
 * command as (LCD_OPCODE_WRITE_CMD << 24) | (cmd << 8) before tx_param.
 * The component does this internally for the init table but exposes no
 * public brightness helper, so we replicate the framing for runtime
 * writes. */
#define CO5300_QSPI_TX_CMD(cmd)  (((uint32_t)0x02 << 24) | ((uint32_t)(cmd) << 8))
#define CO5300_CMD_BRIGHTNESS    0x51

lv_display_t *amoled_co5300_175_driver_init(void)
{
    if (s_display != NULL) {
        return s_display;
    }

    /* QSPI bus — single host carrying 4 data lines + SCLK + CS. data4-7
     * must be -1 (we're quad, not octal) or the SPI driver tries to claim
     * GPIO 0 and warns about a conflict. */
    const spi_bus_config_t buscfg = SH8601_PANEL_BUS_QSPI_CONFIG(
        PIN_NUM_LCD_SCLK,
        PIN_NUM_LCD_D0, PIN_NUM_LCD_D1, PIN_NUM_LCD_D2, PIN_NUM_LCD_D3,
        LCD_H_RES * 80 * sizeof(uint16_t));
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &buscfg, SPI_DMA_CH_AUTO));

    /* Panel IO over QSPI. Stashed in s_io_handle for runtime brightness
     * writes via amoled_co5300_175_set_brightness_pct. */
    esp_lcd_panel_io_handle_t io_handle = NULL;
    const esp_lcd_panel_io_spi_config_t io_config =
        SH8601_PANEL_IO_QSPI_CONFIG(PIN_NUM_LCD_CS, NULL, NULL);
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(
        (esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config, &io_handle));
    s_io_handle = io_handle;

    /* Panel — vendor_config carries the init register table + QSPI flag. */
    const sh8601_vendor_config_t vendor_config = {
        .init_cmds = s_co5300_init_cmds,
        .init_cmds_size = sizeof(s_co5300_init_cmds) / sizeof(s_co5300_init_cmds[0]),
        .flags = {
            .use_qspi_interface = 1,
        },
    };
    const esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = PIN_NUM_LCD_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = LCD_BIT_PER_PIXEL,
        .vendor_config = (void *)&vendor_config,
    };
    esp_lcd_panel_handle_t panel_handle = NULL;
    ESP_ERROR_CHECK(esp_lcd_new_panel_sh8601(io_handle, &panel_config, &panel_handle));
    s_panel_handle = panel_handle;

    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel_handle));
    /* CO5300's visible column window starts at x=6 (the vendor BSP sets
     * CASET base 0x06 and esp_lcd_panel_set_gap(panel, 0x06, 0)). Without
     * the gap, the 6 panel-native columns at the visible edge are never
     * written and appear as a stale-pixel band. */
    ESP_ERROR_CHECK(esp_lcd_panel_set_gap(panel_handle, 6, 0));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel_handle, true));

    /* LVGL port — 40-row stripe buffers (see disp_cfg below), DMA-
     * friendly, RGB565 with the byte swap LVGL's RGB565 format needs for
     * big-endian-on-wire SPI. */
    const lvgl_port_cfg_t lvgl_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    ESP_ERROR_CHECK(lvgl_port_init(&lvgl_cfg));

    /* 40-row stripe → 3 buffers × 466 × 40 × 2 B = ~109 KB in internal
     * RAM. PSRAM-backed buffers cause DMA TX underflows at the panel's
     * QSPI clock, so keep them in DRAM. */
    const lvgl_port_display_cfg_t disp_cfg = {
        .io_handle = io_handle,
        .panel_handle = panel_handle,
        .buffer_size = LCD_H_RES * 40,
        .double_buffer = true,
        .hres = LCD_H_RES,
        .vres = LCD_V_RES,
        .monochrome = false,
        .rotation = {
            .swap_xy = false,
            .mirror_x = false,
            .mirror_y = false,
        },
        .color_format = LV_COLOR_FORMAT_RGB565,
        .flags = {
            .buff_dma = true,
            .swap_bytes = true,
            /* CO5300 has no hardware swap_xy, so rotation (if any) runs in
             * LVGL. Kept enabled so the fixed rotation below — or a future
             * change to it — works without reconfiguring buffers. */
            .sw_rotate = true,
        },
    };
    s_display = lvgl_port_add_disp(&disp_cfg);
    if (s_display == NULL) {
        ESP_LOGE(TAG, "lvgl_port_add_disp failed");
        return NULL;
    }

    /* Boot rotation baseline. orientation.c (QMI8658 auto-rotate) drives
     * this at runtime once the IMU is up; ROTATION_0 is the pose the
     * panel renders upright at on this board (verified on hardware), and
     * also the fixed orientation when CONFIG_BURNSCOPE_AMOLED_ORIENTATION_AUTO
     * is disabled. The orientation task seeds its state machine to match
     * (AXIS_Y_NEG → ROTATION_0). */
    if (lvgl_port_lock(0)) {
        lv_display_set_rotation(s_display, LV_DISPLAY_ROTATION_0);
        lvgl_port_unlock();
    }
    return s_display;
}

/* The burn-in adapter's drain task and fade-step timer both call into
 * panel-IO outside the LVGL flush pipeline. Holding the same lock LVGL
 * takes around its own panel-IO accesses serialises us against the flush
 * callback and prevents an SPI transaction-done mix-up that would wedge
 * LVGL (see the 1.43" profile's driver.c for the full failure analysis). */
void amoled_co5300_175_set_brightness_pct(uint8_t pct)
{
    if (s_io_handle == NULL) {
        return;
    }
    if (pct > 100) {
        pct = 100;
    }
    const uint8_t reg = (uint8_t)((uint32_t)pct * 255u / 100u);
    const uint32_t framed = CO5300_QSPI_TX_CMD(CO5300_CMD_BRIGHTNESS);
    if (!lvgl_port_lock(0)) {
        ESP_LOGW(TAG, "set_brightness_pct(%u): lvgl_port_lock failed", (unsigned)pct);
        return;
    }
    esp_err_t err = esp_lcd_panel_io_tx_param(s_io_handle, framed, &reg, 1);
    lvgl_port_unlock();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "set_brightness_pct(%u): tx_param failed: %s",
                 (unsigned)pct, esp_err_to_name(err));
    }
}

void amoled_co5300_175_set_display_on(bool on)
{
    if (s_panel_handle == NULL) {
        return;
    }
    if (!lvgl_port_lock(0)) {
        ESP_LOGW(TAG, "set_display_on(%d): lvgl_port_lock failed", (int)on);
        return;
    }
    esp_err_t err = esp_lcd_panel_disp_on_off(s_panel_handle, on);
    lvgl_port_unlock();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "set_display_on(%d): disp_on_off failed: %s",
                 (int)on, esp_err_to_name(err));
    }
}
