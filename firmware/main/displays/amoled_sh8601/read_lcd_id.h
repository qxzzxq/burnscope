#pragma once

#include <stdint.h>

/*
 * One-byte read of RDID1 (register 0xDA) over bit-banged single-line
 * SPI on the QSPI panel pins. Distinguishes the two silicon variants
 * Waveshare dual-sources on the 1.43" round AMOLED:
 *
 *   0x86 — SH8601 (use x_gap=0, the sh8601 init table)
 *   0xFF — CO5300 (use x_gap=6, the co5300 init table; CO5300 doesn't
 *          implement RDID1 so the line floats high under the pull-up)
 *
 * Must be called BEFORE `spi_bus_initialize()` — it drives the same
 * pins (CS/SCLK/D0–D3/RST) as the QSPI bus. Cost: three 120 ms reset
 * settling waits + a few ms of bit-banging.
 *
 * Ported from Waveshare's ESP-IDF demo
 * (`ESP-IDF/07_LVGL_Test/components/read_lcd_id_bsp/read_lcd_id_bsp.c`).
 */
uint8_t amoled_sh8601_read_lcd_id(void);
