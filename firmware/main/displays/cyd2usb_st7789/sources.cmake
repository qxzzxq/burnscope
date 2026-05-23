if(CONFIG_BURNSCOPE_DISPLAY_CYD2USB_ST7789)
    list(APPEND srcs
        "displays/cyd2usb_st7789/driver.c"
        "displays/cyd2usb_st7789/ui.c"
    )
    list(APPEND inc "displays/cyd2usb_st7789")
endif()
