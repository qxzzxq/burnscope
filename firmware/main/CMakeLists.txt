set(srcs
    "main.c"
    "wifi.c"
    "mdns_svc.c"
    "ntp.c"
    "http_server.c"
    "watchdog.c"
    "nvs_store.c"
    "snapshot.c"
    "burn_protection/burn_idle.c"
    "factory_reset.c"
    "provisioning.c"
    "captive_dns.c"
    "icons/icon_claude.c"
    "icons/icon_codex.c"
)

set(inc ".")

# Build-time-selected display profile. Each profile under displays/<name>/
# owns its own sources.cmake fragment (and Kconfig fragment, rsource'd from
# Kconfig.projbuild) so adding a new screen is a directory drop plus one
# rsource line — no edits to this file. Note: CONFIGURE_DEPENDS is omitted
# because ESP-IDF re-runs this CMakeLists in script mode during requirements
# extraction, where CONFIGURE_DEPENDS is invalid. After dropping in a new
# profile, run `idf.py reconfigure` (menuconfig also triggers this).
file(GLOB display_profile_fragments
     LIST_DIRECTORIES false
     "${CMAKE_CURRENT_SOURCE_DIR}/displays/*/sources.cmake")
foreach(fragment IN LISTS display_profile_fragments)
    include(${fragment})
endforeach()

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
