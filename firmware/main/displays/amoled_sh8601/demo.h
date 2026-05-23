#pragma once

/*
 * Driver-verification demo for the Waveshare 1.43" round AMOLED panel
 * (SH8601 or CO5300 silicon — both speak the same QSPI protocol and
 * the demo doesn't care which is wired). Renders a static test
 * pattern (color bars, corner markers, centre crosshair) and returns.
 * Caller is expected to sleep forever afterwards — the
 * Wi-Fi / NVS / HTTP stack does NOT run in demo mode.
 *
 * Built only when `CONFIG_BURNSCOPE_AMOLED_DEMO=y`.
 */
void amoled_demo_run(void);
