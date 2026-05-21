# burnscope-client (v2)

Per-laptop client that reports Claude Code and Codex CLI rate-limit usage to
the BurnScope ESP32. v2 drops the v1 header-probe approach in favor of two
agent-native, zero-cost sources:

- **Claude** — Claude Code statusline hook (`claude_statusline.py`,
  invoked per fire).
- **Codex** — `codex app-server` JSON-RPC subprocess (`codex_daemon.py`,
  long-lived).

See `docs/client-spec-v2.html` for the full specification. The v1 client is
preserved at `../client_old/` for reference.

## Install

```
pip install -e .
burnscope install claude    # patches ~/.claude/settings.json
burnscope install codex     # macOS launchd or Linux systemd --user
burnscope status            # confirm wiring
```
