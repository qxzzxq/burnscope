#include "device_id.h"

#include <stdio.h>
#include <string.h>

#include "esp_err.h"
#include "esp_mac.h"

const char *device_id_str(void)
{
    /* Lazy one-shot cache. A race between two tasks on the very first
     * call is benign: both compute the same string from the same MAC
     * and write identical bytes. Subsequent reads are then a plain
     * pointer return. */
    static char cached[DEVICE_ID_MAX] = "";
    if (cached[0] == '\0') {
        uint8_t mac[6] = { 0 };
        ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_WIFI_STA));
        snprintf(cached, sizeof(cached), "burnscope-%02x%02x",
                 mac[4], mac[5]);
    }
    return cached;
}
