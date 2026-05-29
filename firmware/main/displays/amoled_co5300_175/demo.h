#pragma once

/*
 * Driver-verification demo for the Waveshare 1.75" round AMOLED panel
 * (CO5300 over QSPI). Renders a static test pattern (color bars, corner
 * markers, centre crosshair) and returns. Caller is expected to sleep
 * forever afterwards — the Wi-Fi / NVS / HTTP stack does NOT run in demo
 * mode.
 *
 * Built only when `CONFIG_BURNSCOPE_AMOLED_DEMO=y`.
 */
void amoled_demo_run(void);
