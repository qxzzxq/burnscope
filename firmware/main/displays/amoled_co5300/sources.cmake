if(CONFIG_BURNSCOPE_DISPLAY_AMOLED_CO5300)
    list(APPEND srcs
        "displays/amoled_co5300/driver.c"
        "displays/amoled_co5300/co5300.c"
        "displays/amoled_co5300/ui.c"
    )
    list(APPEND inc "displays/amoled_co5300")
endif()
