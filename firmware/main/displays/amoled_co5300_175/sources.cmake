if(CONFIG_BURNSCOPE_DISPLAY_AMOLED_CO5300_175)
    list(APPEND srcs
        "displays/amoled_co5300_175/driver.c"
        "displays/amoled_co5300_175/ui.c"
        "displays/amoled_co5300_175/touch.c"
        "displays/amoled_co5300_175/qmi8658.c"
        "displays/amoled_co5300_175/burn_idle_adapter.c"
        "displays/amoled_co5300_175/orientation.c"
        # 70×70 brand icons are AMOLED-only — same assets the 1.43"
        # profile uses (the cyd2usb profile uses the 24×24 variants).
        "icons/icon_claude_70.c"
        "icons/icon_codex_70.c"
    )
    list(APPEND inc "displays/amoled_co5300_175")

    if(CONFIG_BURNSCOPE_AMOLED_DEMO)
        list(APPEND srcs "displays/amoled_co5300_175/demo.c")
    endif()
endif()
