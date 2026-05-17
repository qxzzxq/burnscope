#!/usr/bin/env bash
set -uo pipefail
# Claude Code stores its OAuth token in the macOS Keychain.
# Linux path: ~/.claude/.credentials.json (uncomment the alternate read below).
CRED=$(security find-generic-password -s "Claude Code-credentials" -a "$USER" -w 2>/dev/null)
# CRED=$(cat ~/.claude/.credentials.json)   # Linux fallback
TOKEN=$(echo "$CRED" | jq -r '.claudeAiOauth.accessToken // .accessToken')
if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
  echo "Failed to extract accessToken from credentials"
  echo "Raw credential blob:"
  echo "$CRED" | head -c 200
  exit 1
fi
curl -sS --max-time 10 \
  -D /tmp/claude-headers.txt -o /tmp/claude-body.txt \
  -X POST https://api.anthropic.com/v1/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "Content-Type: application/json" \
  -H "User-Agent: claude-code/2.1.5" \
  -d '{
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}]
  }' || true
echo "--- status ---"
head -1 /tmp/claude-headers.txt
echo "--- rate-limit headers ---"
grep -i '^anthropic-ratelimit-' /tmp/claude-headers.txt || echo "(none found)"
echo "--- body (first 400 bytes) ---"
head -c 400 /tmp/claude-body.txt; echo
