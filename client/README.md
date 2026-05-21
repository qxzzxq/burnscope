# burnscope-client (v2)

Per-laptop client that reports Claude Code and Codex CLI rate-limit usage to
the BurnScope ESP32. v2 drops the v1 header-probe approach in favor of two
agent-native, zero-cost sources:

- **Claude** — Claude Code statusline hook (`claude_statusline.py`,
  invoked per fire).
- **Codex** — `codex app-server` JSON-RPC subprocess (`codex_daemon.py`,
  long-lived).

See `docs/client-spec-v2.html` for the full specification. The v1 client
was removed; its history is preserved in git.

## Install

```
pip install -e .
burnscope install claude    # patches ~/.claude/settings.json
burnscope install codex     # macOS launchd or Linux systemd --user
burnscope status            # confirm wiring
```

## Debugging

Claude Code discards the statusline script's stderr, so logs are silent
by default. Set `BURNSCOPE_LOG_FILE` to capture them:

```
export BURNSCOPE_LOG_FILE=~/.burnscope/claude.log
# trigger a Claude message; then:
tail -f ~/.burnscope/claude.log
```

The env var is inherited by the detached `--push` child, so both the
foreground render and the network call land in the same file. The codex
daemon honors the same variable; when unset, it falls back to stderr
(which the launchd plist / systemd unit redirects to
`~/.burnscope/codex.stderr.log`).

By default the log level is `INFO` — only lifecycle events and warnings
are recorded. For the full per-fire narrative (mDNS browse, cache hits,
POST URL, etc.) bump it up:

```
export BURNSCOPE_LOG_LEVEL=DEBUG
```

Accepted values: `DEBUG`, `INFO` (default), `WARNING`, `ERROR`
(case-insensitive). Unknown values fall back to `INFO`.

