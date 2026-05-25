#pragma once

#include "esp_err.h"

/* LittleFS mount for the `storage` partition (only declared in the
 * 16 MB AMOLED layout; the 4 MB CYD layout has no such partition).
 *
 * Call once during app_main, after NVS is up. Idempotent: a second
 * call is a no-op. If the partition is absent (e.g. CYD profile),
 * returns ESP_ERR_NOT_FOUND and logs an info-level note — every
 * other path treats the filesystem as optional, so this is not fatal.
 *
 * Files are accessible under /storage/... via the standard POSIX
 * fopen/open APIs once mounted.
 */
esp_err_t storage_mount(void);

/* Compile-time mount point. Use this in writers so a future rename
 * is a single-file edit. */
#define STORAGE_MOUNT_POINT  "/storage"
