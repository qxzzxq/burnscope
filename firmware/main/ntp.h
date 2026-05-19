#pragma once

/*
 * ntp.h — SNTP wall-clock sync against `pool.ntp.org`.
 *
 * Phase 1 only needs the wall clock for `Date:` headers and (Phase 2) the
 * countdown timer. Default poll interval (1 h) is configurable via
 * CONFIG_LWIP_SNTP_UPDATE_DELAY; we keep the IDF default.
 */

/**
 * Initialise the SNTP client and kick off the first sync. Idempotent —
 * call once after `IP_EVENT_STA_GOT_IP`; subsequent calls are no-ops.
 */
void ntp_start(void);
