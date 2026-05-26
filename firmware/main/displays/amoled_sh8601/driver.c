/*
 * Waveshare ESP32-S3-Touch-AMOLED-1.43 bring-up.
 *
 * Uses the managed component `espressif/esp_lcd_sh8601` which supports
 * the QSPI command framing (cmd=0x02 + 24-bit-address phase carrying
 * the real command) that this AMOLED family expects. The Waveshare 1.43
 * board ships with either an SH8601 or a CO5300 driver IC — both speak
 * the same QSPI protocol, so this driver works for either.
 *
 * Pin assignments verified against the Waveshare 1.43 schematic
 * (GPIO/AMOLED column): see #define block below.
 *
 * Touch: FT3168 (I²C on IO47/IO48), polled — see touch.c. Wired as
 * a burn-in wake source via the LVGL indev callback in ui.c.
 */

#include "driver.h"

#include "esp_err.h"
#include "esp_log.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_sh8601.h"
#include "esp_lvgl_port.h"
#include "nvs_store.h"
#include "read_lcd_id.h"

/* RDID1 (0xDA) values observed on the Waveshare 1.43" dual-sourced
 * panel. CO5300 doesn't implement RDID1, so the line floats high
 * under the pull-up — we treat 0xFF as the CO5300 signature. */
#define SH8601_RDID1   0x86
#define CO5300_RDID1   0xFF

static const char *TAG = "amoled_drv";

/* Valid RDID1 values for the dual-sourced Waveshare 1.43" AMOLED panel.
 * SH8601 responds 0x86; CO5300 floats high (0xFF). Anything else is a
 * glitched read or unknown silicon and must not be persisted.
 *
 * Named without a leading underscore: file-scope identifiers starting
 * with an underscore are reserved by C11 §7.1.3.
 */
static bool lcd_id_is_valid(uint8_t id)
{
    return id == SH8601_RDID1 || id == CO5300_RDID1;
}

/* Probe RDID1, prefer NVS cache. Falls back to a fresh bit-bang on
 * cache miss and persists the result via the typed nvs_store wrapper
 * (every persisted byte routes through nvs_store for auditability).
 *
 * Only recognised values (0x86 / 0xFF) are cached; a glitched read
 * that returns any other byte is logged and discarded — the next boot
 * re-reads the panel rather than trusting a corrupt NVS entry forever. */
static uint8_t resolve_lcd_id(void)
{
    uint8_t cached = 0;
    if (nvs_store_load_lcd_id(&cached) == ESP_OK) {
        if (lcd_id_is_valid(cached)) {
            ESP_LOGI(TAG, "LCD ID from NVS cache: 0x%02x", cached);
            return cached;
        }
        ESP_LOGW(TAG, "NVS-cached LCD ID 0x%02x is unrecognised; re-reading", cached);
    }
    const uint8_t id = amoled_sh8601_read_lcd_id();
    if (lcd_id_is_valid(id)) {
        (void)nvs_store_save_lcd_id(id);
    } else {
        ESP_LOGW(TAG, "LCD RDID1 0x%02x is unrecognised; not caching", id);
    }
    return id;
}

#define LCD_HOST            SPI2_HOST
#define PIN_NUM_LCD_SCLK    10   /* OLED_CLK  */
#define PIN_NUM_LCD_D0      11   /* OLED_SIO0 */
#define PIN_NUM_LCD_D1      12   /* OLED_SI1  */
#define PIN_NUM_LCD_D2      13   /* OLED_SI2  */
#define PIN_NUM_LCD_D3      14   /* OLED_SI3  */
#define PIN_NUM_LCD_CS      9    /* OLED_CS   */
#define PIN_NUM_LCD_RST     21   /* OLED_RESET */

#define LCD_H_RES           466
#define LCD_V_RES           466
#define LCD_BIT_PER_PIXEL   16
/* 80 MHz is the speed Arduino_GFX uses on this exact board. */
#define LCD_PIXEL_CLOCK_HZ  (80 * 1000 * 1000)

/* Init register sequences mirrored from Waveshare's own ESP-IDF demo
 * (`ESP-IDF/07_LVGL_Test/main/example_qspi_with_ram.c`). The board
 * ships with either an SH8601 or a CO5300 driver IC; we pick the
 * matching table at runtime after reading RDID1 (see read_lcd_id.c).
 *
 * Both sequences include a brightness ramp (0x51=0x00 before DISPON,
 * then 0x51=0xB2 after) to suppress the framebuffer-junk flash during
 * LVGL's first frame. The end-of-ramp value is the OLED-burn-in FSD's
 * DEFAULT_BRIGHTNESS of 70 % (0xB2 = 178/255 ≈ 70 %) rather than the
 * vendor demo's 0xFF — burning the panel at 100 % until the Phase-2
 * idle adapter takes over would defeat the burn-in story.
 *
 * TE registers (0x44 scanline target, 0x35 ON) and MADCTL (0x36) are
 * omitted because we don't wire the TE line to GPIO and we do
 * software rotation in LVGL instead of hardware rotation.
 */
#define SH8601_DEFAULT_BRIGHTNESS  0xB2  /* ≈ 70 % per OLED-burn-in FSD § A3 */

static const sh8601_lcd_init_cmd_t s_sh8601_init_cmds[] = {
    { 0x11, NULL, 0, 120 },                    /* SLPOUT, 120ms settle */
    { 0x53, (uint8_t[]){ 0x20 }, 1, 10 },      /* WCTRLD1 */
    { 0x51, (uint8_t[]){ 0x00 }, 1, 10 },      /* brightness 0 before DISPON */
    { 0x29, NULL, 0, 10 },                     /* DISPON */
    { 0x51, (uint8_t[]){ SH8601_DEFAULT_BRIGHTNESS }, 1, 0 }, /* ramp to 70 % */
};

static const sh8601_lcd_init_cmd_t s_co5300_init_cmds[] = {
    { 0x11, NULL, 0, 80 },                     /* SLPOUT, 80ms settle */
    { 0xC4, (uint8_t[]){ 0x80 }, 1, 0 },       /* SPIMODECTL: stay in QSPI */
    { 0x53, (uint8_t[]){ 0x20 }, 1, 1 },       /* WCTRLD1 */
    { 0x63, (uint8_t[]){ 0xFF }, 1, 1 },       /* HBM ceiling max (only used when WCTRLD HBM=1) */
    { 0x51, (uint8_t[]){ 0x00 }, 1, 1 },       /* brightness 0 before DISPON */
    { 0x29, NULL, 0, 10 },                     /* DISPON */
    { 0x51, (uint8_t[]){ SH8601_DEFAULT_BRIGHTNESS }, 1, 0 }, /* ramp to 70 % */
};

static lv_display_t              *s_display      = NULL;
static esp_lcd_panel_handle_t     s_panel_handle = NULL;
static esp_lcd_panel_io_handle_t  s_io_handle    = NULL;

/* SH8601 QSPI command framing: the managed component wraps each command
 * as (LCD_OPCODE_WRITE_CMD << 24) | (cmd << 8) before tx_param. The
 * component does this internally for the init table, but exposes no
 * public brightness helper, so we replicate the framing for runtime
 * writes. See esp_lcd_sh8601.c:142 (`tx_param`). */
#define SH8601_QSPI_TX_CMD(cmd)  (((uint32_t)0x02 << 24) | ((uint32_t)(cmd) << 8))
#define SH8601_CMD_BRIGHTNESS    0x51

lv_display_t *amoled_sh8601_driver_init(void)
{
    if (s_display != NULL) {
        return s_display;
    }

    /* Detect the silicon variant — NVS cache on subsequent boots,
     * bit-banged RDID1 read on first boot. See read_lcd_id.c. */
    const uint8_t lcd_id = resolve_lcd_id();
    const bool is_sh8601 = (lcd_id == SH8601_RDID1);
    ESP_LOGI(TAG, "Detected panel silicon: %s (RDID1=0x%02x)",
             is_sh8601 ? "SH8601" : "CO5300", lcd_id);

    /* QSPI bus — single host carrying 4 data lines + SCLK + CS. data4-7
     * must be -1 (we're quad, not octal) or the SPI driver tries to claim
     * GPIO 0 and warns about a conflict. */
    const spi_bus_config_t buscfg = SH8601_PANEL_BUS_QSPI_CONFIG(
        PIN_NUM_LCD_SCLK,
        PIN_NUM_LCD_D0, PIN_NUM_LCD_D1, PIN_NUM_LCD_D2, PIN_NUM_LCD_D3,
        LCD_H_RES * 80 * sizeof(uint16_t));
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &buscfg, SPI_DMA_CH_AUTO));

    /* Panel IO over QSPI — the SH8601 component knows how to wrap each
     * command in the cmd=0x02 / addr=(cmd<<8) framing the panel expects.
     * Stashed in s_io_handle for runtime brightness writes via
     * amoled_sh8601_set_brightness_pct (which replicates the framing). */
    esp_lcd_panel_io_handle_t io_handle = NULL;
    const esp_lcd_panel_io_spi_config_t io_config =
        SH8601_PANEL_IO_QSPI_CONFIG(PIN_NUM_LCD_CS, NULL, NULL);
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(
        (esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config, &io_handle));
    s_io_handle = io_handle;

    /* Panel — vendor_config carries the init register table + QSPI flag.
     * Pick the SH8601 or CO5300 sequence based on the RDID1 read. */
    const sh8601_vendor_config_t vendor_config = {
        .init_cmds = is_sh8601 ? s_sh8601_init_cmds : s_co5300_init_cmds,
        .init_cmds_size = is_sh8601
            ? sizeof(s_sh8601_init_cmds) / sizeof(s_sh8601_init_cmds[0])
            : sizeof(s_co5300_init_cmds) / sizeof(s_co5300_init_cmds[0]),
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
    /* CO5300's visible column window starts at x=6; SH8601 starts at
     * x=0. Without the correct gap, the 6 panel-native columns at the
     * visible right edge are never written, which appears as a stale-
     * pixel band at the top of the screen after our 270° rotation. */
    ESP_ERROR_CHECK(esp_lcd_panel_set_gap(panel_handle, is_sh8601 ? 0 : 6, 0));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel_handle, true));

    /* LVGL port — 40-row stripe buffers (see disp_cfg below), DMA-
     * friendly, RGB565 with the byte swap LVGL's RGB565 format needs
     * for big-endian-on-wire SPI. */
    const lvgl_port_cfg_t lvgl_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    ESP_ERROR_CHECK(lvgl_port_init(&lvgl_cfg));

    /* 40-row stripe → 3 buffers × 466 × 40 × 2 B = ~109 KB in internal
     * RAM. Waveshare's demo uses MALLOC_CAP_DMA (internal RAM) with two
     * 116-row stripes; LVGL 9 + sw_rotate adds a third scratch buffer,
     * so we shrink the stripe to keep all three in DRAM. PSRAM-backed
     * buffers cause DMA TX underflows at the panel's QSPI clock. */
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
            /* SH8601 / CO5300 has no hardware swap_xy, so we rotate in
             * LVGL. Matches Waveshare's own demo
             * (`disp_drv.sw_rotate = 1; disp_drv.rotated = LV_DISP_ROT_270`). */
            .sw_rotate = true,
        },
    };
    s_display = lvgl_port_add_disp(&disp_cfg);
    if (s_display == NULL) {
        ESP_LOGE(TAG, "lvgl_port_add_disp failed");
        return NULL;
    }

    /* 270° rotation puts logical (0,0) at the panel's physical bottom-
     * right when the USB-C connector is at the bottom of the board.
     * Equivalent to LV_DISP_ROT_270 in the Waveshare LVGL-8 demo. */
    if (lvgl_port_lock(0)) {
        lv_display_set_rotation(s_display, LV_DISPLAY_ROTATION_270);
        lvgl_port_unlock();
    }
    return s_display;
}

void amoled_sh8601_set_brightness_pct(uint8_t pct)
{
    if (s_io_handle == NULL) {
        return;
    }
    if (pct > 100) {
        pct = 100;
    }
    const uint8_t reg = (uint8_t)((uint32_t)pct * 255u / 100u);
    const uint32_t framed = SH8601_QSPI_TX_CMD(SH8601_CMD_BRIGHTNESS);
    esp_err_t err = esp_lcd_panel_io_tx_param(s_io_handle, framed, &reg, 1);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "set_brightness_pct(%u): tx_param failed: %s",
                 (unsigned)pct, esp_err_to_name(err));
    }
}

void amoled_sh8601_set_display_on(bool on)
{
    if (s_panel_handle == NULL) {
        return;
    }
    esp_err_t err = esp_lcd_panel_disp_on_off(s_panel_handle, on);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "set_display_on(%d): disp_on_off failed: %s",
                 (int)on, esp_err_to_name(err));
    }
}
