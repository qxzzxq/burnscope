if(CONFIG_BURNSCOPE_DISPLAY_AMOLED_SH8601)
    list(APPEND srcs
        "displays/amoled_sh8601/driver.c"
        "displays/amoled_sh8601/read_lcd_id.c"
        "displays/amoled_sh8601/ui.c"
    )
    list(APPEND inc "displays/amoled_sh8601")

    if(CONFIG_BURNSCOPE_AMOLED_DEMO)
        list(APPEND srcs "displays/amoled_sh8601/demo.c")
    endif()
endif()
