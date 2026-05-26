/*
 * LittleFS mount for the `storage` partition.
 *
 * The 16 MB AMOLED partition layout reserves ~5.8 MB at offset 0xa20000
 * as a LittleFS volume. We mount it at boot and log the geometry so the
 * partition is immediately writable by any future module (pixel-aging
 * map, snapshot history, cached assets). On the 4 MB CYD layout the
 * `storage` partition is absent — `esp_vfs_littlefs_register` returns
 * ESP_ERR_NOT_FOUND and we log + continue rather than failing app_main.
 *
 * `format_if_mount_failed=true` so the first boot on a freshly-flashed
 * board doesn't need a manual format pass; LittleFS handles power-fail
 * during format/mount idempotently.
 */

#include "storage.h"

#include "esp_littlefs.h"
#include "esp_log.h"

static const char *TAG = "storage";
static const char  PARTITION_LABEL[] = "storage";

esp_err_t storage_mount(void)
{
    if (esp_littlefs_mounted(PARTITION_LABEL)) {
        return ESP_OK;
    }

    const esp_vfs_littlefs_conf_t conf = {
        .base_path              = STORAGE_MOUNT_POINT,
        .partition_label        = PARTITION_LABEL,
        .format_if_mount_failed = true,
        .dont_mount             = false,
    };

    esp_err_t err = esp_vfs_littlefs_register(&conf);
    if (err == ESP_ERR_NOT_FOUND) {
        ESP_LOGI(TAG, "no `storage` partition declared in this layout — "
                      "filesystem unavailable (expected on the CYD profile)");
        return err;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LittleFS mount failed: %s", esp_err_to_name(err));
        return err;
    }

    size_t total = 0, used = 0;
    esp_err_t info_err = esp_littlefs_info(PARTITION_LABEL, &total, &used);
    if (info_err == ESP_OK) {
        ESP_LOGI(TAG, "LittleFS mounted at %s — %u KiB total, %u KiB used (%.1f%% free)",
                 STORAGE_MOUNT_POINT,
                 (unsigned)(total / 1024),
                 (unsigned)(used / 1024),
                 100.0 * (double)(total - used) / (double)total);
    } else {
        ESP_LOGW(TAG, "LittleFS mounted at %s; size query failed: %s",
                 STORAGE_MOUNT_POINT, esp_err_to_name(info_err));
    }
    return ESP_OK;
}
