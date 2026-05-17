#!/usr/bin/env bash
set -uo pipefail   # no -e: a curl timeout returns non-zero, that's expected

AUTH=~/.codex/auth.json
TOKEN=$(jq -r '.tokens.access_token' "$AUTH")
ACCOUNT=$(jq -r '.tokens.account_id // empty' "$AUTH")

ACCOUNT_HEADER=()
if [ -n "$ACCOUNT" ]; then
  ACCOUNT_HEADER=(-H "ChatGPT-Account-ID: $ACCOUNT")
fi

curl -sS --max-time 10 -N \
  -D /tmp/codex-headers.txt -o /tmp/codex-body.txt \
  -X POST https://chatgpt.com/backend-api/codex/responses \
  -H "Authorization: Bearer $TOKEN" \
  "${ACCOUNT_HEADER[@]}" \
  -H "Content-Type: application/json" \
  -H "OpenAI-Beta: responses=experimental" \
  -d '{
    "model": "gpt-5.5",
    "instructions": "",
    "input": [
      {"type": "message", "role": "user",
       "content": [{"type": "input_text", "text": "hi"}]}
    ],
    "tools": [],
    "tool_choice": "auto",
    "parallel_tool_calls": false,
    "store": false,
    "stream": true,
    "include": []
  }' || true

echo "--- status ---"
head -1 /tmp/codex-headers.txt
echo "--- rate-limit headers ---"
grep -i '^x-codex-' /tmp/codex-headers.txt || echo "(none found)"
echo "--- all headers ---"
cat /tmp/codex-headers.txt
echo "--- body (first 400 bytes) ---"
cat /tmp/codex-body.txt