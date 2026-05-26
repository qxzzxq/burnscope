if(CONFIG_BURNSCOPE_DISPLAY_AMOLED_SH8601)
    list(APPEND srcs
        "displays/amoled_sh8601/driver.c"
        "displays/amoled_sh8601/read_lcd_id.c"
        "displays/amoled_sh8601/ui.c"
        "displays/amoled_sh8601/touch.c"
        "displays/amoled_sh8601/qmi8658.c"
        "displays/amoled_sh8601/burn_idle_adapter.c"
        # 70×70 brand icons are AMOLED-only — keep them out of the
        # CYD binary (the cyd2usb profile uses the 24×24 variants in
        # main/icons/icon_{claude,codex}.c which stay in the shared
        # srcs list).
        "icons/icon_claude_70.c"
        "icons/icon_codex_70.c"
    )
    list(APPEND inc "displays/amoled_sh8601")

    if(CONFIG_BURNSCOPE_AMOLED_DEMO)
        list(APPEND srcs "displays/amoled_sh8601/demo.c")
    endif()
endif()
