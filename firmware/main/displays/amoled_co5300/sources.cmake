if(CONFIG_BURNSCOPE_DISPLAY_AMOLED_CO5300)
    list(APPEND srcs
        "displays/amoled_co5300/driver.c"
        "displays/amoled_co5300/ui.c"
    )
    list(APPEND inc "displays/amoled_co5300")

    if(CONFIG_BURNSCOPE_AMOLED_DEMO)
        list(APPEND srcs "displays/amoled_co5300/demo.c")
    endif()
endif()
