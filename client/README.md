# burnscope-client (v2)

The laptop-side client that reports Claude Code and Codex CLI rate-limit usage
to the BurnScope ESP32. v2 replaces v1's header probing with two zero-cost
sources the agents already produce:

- **Claude**: a Claude Code statusline hook (`claude_statusline.py`), run on each fire.
- **Codex**: a long-lived `codex app-server` JSON-RPC subprocess (`codex_daemon.py`).

Full spec: `docs/client-spec-v2.html`. The v1 client is gone; its history is in git.

## Install

This project uses [uv](https://docs.astral.sh/uv/); `uv.lock` pins the deps.

For normal use, install `burnscope` onto your PATH and call it directly:

```sh
cd client
uv tool install .
burnscope install claude    # patches ~/.claude/settings.json
burnscope install codex     # macOS launchd or Linux systemd --user
burnscope status            # confirm wiring
```

For development, work from a local `.venv/` instead. It isn't on your PATH, so
run the same CLI through `uv run`:

```sh
cd client
uv sync
uv run burnscope status     # same command, via the project venv
uv run pytest               # run the test suite
```

## Debugging

Claude Code discards the statusline script's stderr, so logs are silent by
default. Set `BURNSCOPE_LOG_FILE` to capture them:

```sh
export BURNSCOPE_LOG_FILE=~/.burnscope/claude.log
# trigger a Claude message, then:
tail -f ~/.burnscope/claude.log
```

The detached `--push` child inherits the variable, so the foreground render and
the network call land in the same file. The codex daemon honors it too; when
unset, it falls back to stderr (which the launchd plist or systemd unit
redirects to `~/.burnscope/codex.stderr.log`).

The default log level is `INFO`, which records only lifecycle events and
warnings. For the full per-fire narrative (mDNS browse, cache hits, POST URL),
raise it:

```sh
export BURNSCOPE_LOG_LEVEL=DEBUG
```

Accepted values: `DEBUG`, `INFO` (default), `WARNING`, `ERROR`
(case-insensitive). Unknown values fall back to `INFO`.
