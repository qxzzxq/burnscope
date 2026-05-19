set(srcs
    "main.c"
    "wifi.c"
    "mdns_svc.c"
    "ntp.c"
    "http_server.c"
    "watchdog.c"
    "nvs_store.c"
    "snapshot.c"
    "factory_reset.c"
    "provisioning.c"
    "captive_dns.c"
    "icons/icon_claude.c"
    "icons/icon_codex.c"
)

set(inc ".")

# Build-time-selected display profile. Each profile bundles its panel
# driver and its LVGL layout because UI design is geometry-bound.
if(CONFIG_BURNSCOPE_DISPLAY_CYD2USB_ST7789)
    list(APPEND srcs
        "displays/cyd2usb_st7789/driver.c"
        "displays/cyd2usb_st7789/ui.c"
    )
    list(APPEND inc "displays/cyd2usb_st7789")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS ${inc}
    REQUIRES
        # Platform + radio + RTOS plumbing.
        nvs_flash esp_wifi esp_netif esp_event esp_timer esp_lcd
        # Networking services. cJSON would belong here in IDF 5.x but
        # has been removed from IDF 6 and the /summary payload is small
        # enough to parse inline.
        esp_http_server lwip
        # Display stack — LVGL + managed mDNS component.
        esp_lvgl_port lvgl__lvgl espressif__mdns
        # FreeRTOS primitives + GPIO + watchdog APIs.
        driver esp_system freertos
)
