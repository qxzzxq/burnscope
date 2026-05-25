#!/usr/bin/env bash
# SM-070: assert that the pure-logic idle module has no firmware-specific
# includes (no ESP-IDF, no FreeRTOS, no LVGL, no panel-driver headers).
#
# Exits:
#   0 — file is clean
#   1 — file exists and contains a forbidden include
#   2 — file argument missing, unreadable, or grep itself errored

set -eu

file="${1:?usage: check_purity.sh <path-to-burn_idle.c>}"

if [[ ! -r "$file" ]]; then
    echo "SM-070 FAIL: file not readable: ${file}" >&2
    exit 2
fi

# Forbidden include patterns. Each must be matchable on a line that
# resembles a #include directive, so we anchor with #include + whitespace.
patterns='^[[:space:]]*#[[:space:]]*include[[:space:]]*[<"]([^>"]*(esp_|freertos/|lvgl|lv_|driver/|esp_lcd_))'

set +e
grep -nE "${patterns}" "${file}" >&2
status=$?
set -e
case "$status" in
    0)
        echo "SM-070 FAIL: forbidden include detected in ${file}" >&2
        exit 1
        ;;
    1)
        echo "SM-070 OK: ${file} is pure."
        ;;
    *)
        echo "SM-070 FAIL: grep errored (status=${status}) against ${file}" >&2
        exit 2
        ;;
esac
