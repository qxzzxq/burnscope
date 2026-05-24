#!/usr/bin/env bash
# SM-070: assert that the pure-logic idle module has no firmware-specific
# includes (no ESP-IDF, no FreeRTOS, no LVGL, no panel-driver headers).
#
# Exits 0 if clean, 1 otherwise. Writes any offending lines to stderr.

set -eu

file="${1:?usage: check_purity.sh <path-to-burn_idle.c>}"

# Forbidden include patterns. Each must be matchable on a line that
# resembles a #include directive, so we anchor with #include + whitespace.
patterns='^[[:space:]]*#[[:space:]]*include[[:space:]]*[<"]([^>"]*(esp_|freertos/|lvgl|lv_|driver/|esp_lcd_))'

if grep -nE "${patterns}" "${file}" >&2; then
    echo "SM-070 FAIL: forbidden include detected in ${file}" >&2
    exit 1
fi

echo "SM-070 OK: ${file} is pure."
