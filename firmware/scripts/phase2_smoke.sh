#!/usr/bin/env bash
# Phase-2 smoke tests against a running BurnScope firmware.
#
# Covers FSD § 8.2: TC-SUM-100/101/102/103/104/105 and TC-HEALTH-100.
# UI-side cases (TC-UI-100/101) require visual inspection — this script
# only proves the wire-side behaviour.
#
# Usage:
#   scripts/phase2_smoke.sh [host]
#
# `host` defaults to `burnscope.local`. Pass the IP if your network blocks
# mDNS. Requires `curl` and `jq`.
set -euo pipefail

HOST="${1:-burnscope.local}"
BASE="http://${HOST}"

note() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '   \033[1;32m✓\033[0m %s\n' "$*"; }
fail() { printf '   \033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

NOW=$(date -u +%s)

# --- TC-SUM-100  happy claude --------------------------------------------
# Mirrors docs/examples/summary-push.json's shape but with fresh
# timestamps so the panel's countdown shows a non-trivial value rather
# than "reset due" once the canned example ages.
note "TC-SUM-100: valid Claude AgentSnapshot ⇒ 204"
CLAUDE_5H=$((NOW + 4 * 3600))
CLAUDE_7D=$((NOW + 5 * 86400))
claude_body=$(cat <<EOF
{"agent":"claude","captured_at":${NOW},
 "sessions":[
   {"type":"current","used_pct":0.03,"resets_at":${CLAUDE_5H}},
   {"type":"weekly","used_pct":0.09,"resets_at":${CLAUDE_7D}}
 ]}
EOF
)
code=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "${BASE}/summary" \
    -H 'Content-Type: application/json' \
    --data-binary "$claude_body")
[[ "$code" == "204" ]] && ok "204 returned" || fail "expected 204, got ${code}"

# --- TC-SUM-101  multi-agent  --------------------------------------------
note "TC-SUM-101: Codex push, then verify both stored in /health"
RESET_5=$((NOW + 3600))
RESET_7=$((NOW + 86400))
codex_body=$(cat <<EOF
{"agent":"codex","captured_at":${NOW},
 "sessions":[
   {"type":"primary","used_pct":0.42,"resets_at":${RESET_5}},
   {"type":"secondary","used_pct":0.81,"resets_at":${RESET_7}}
 ]}
EOF
)
code=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "${BASE}/summary" \
    -H 'Content-Type: application/json' \
    --data-binary "$codex_body")
[[ "$code" == "204" ]] && ok "codex push 204" || fail "expected 204, got ${code}"

agents=$(curl -fsS "${BASE}/health" | jq -r '.agents | keys | sort | join(",")')
[[ "$agents" == "claude,codex" ]] \
    && ok "both agents stored: ${agents}" \
    || fail "expected claude,codex; got ${agents}"

# --- TC-SUM-102  verbatim labels  ----------------------------------------
note "TC-SUM-102: codex labels render verbatim — confirm visually on the panel"
ok "(visual check only)"

# --- TC-SUM-103  bad JSON  -----------------------------------------------
note "TC-SUM-103: invalid JSON ⇒ 400"
code=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "${BASE}/summary" \
    -H 'Content-Type: application/json' \
    --data-binary 'not json')
[[ "$code" == "400" ]] && ok "400 returned" || fail "expected 400, got ${code}"

# --- TC-SUM-104  out-of-range used_pct  ----------------------------------
note "TC-SUM-104: used_pct=1.5 ⇒ 400"
oor_body=$(cat <<EOF
{"agent":"claude","captured_at":${NOW},
 "sessions":[{"type":"current","used_pct":1.5,"resets_at":${RESET_5}}]}
EOF
)
code=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "${BASE}/summary" \
    -H 'Content-Type: application/json' \
    --data-binary "$oor_body")
[[ "$code" == "400" ]] && ok "400 returned" || fail "expected 400, got ${code}"

# --- TC-SUM-105  oversized body  -----------------------------------------
note "TC-SUM-105: 20 KiB body ⇒ 413"
big=$(head -c 20480 /dev/zero | tr '\0' 'x')
code=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "${BASE}/summary" \
    -H 'Content-Type: application/json' \
    --data-binary "$big")
[[ "$code" == "413" ]] && ok "413 returned" || fail "expected 413, got ${code}"

# --- TC-HEALTH-100  shape  -----------------------------------------------
note "TC-HEALTH-100: /health JSON shape"
hjson=$(curl -fsS "${BASE}/health")
for field in firmware_version uptime_s free_heap_b agents; do
    echo "$hjson" | jq -e ".${field}" >/dev/null \
        || fail "missing field: ${field}"
done
echo "$hjson" | jq -e '.agents.claude.seconds_since_last_push' >/dev/null \
    || fail "claude.seconds_since_last_push missing"
ok "shape OK"

printf '\n\033[1;32mAll wire-side Phase-2 smoke tests passed.\033[0m\n'
printf 'Remember to also verify TC-UI-100 (visual layout) and TC-UI-101 (countdown ticks).\n'
