# BurnScope

An always-on token & quota meter for your AI coding agents.

This repo has two components:

- `client/` — Python 3.11+ daemon that probes each supported agent and pushes snapshots to the ESP32.
- `firmware/` — ESP32 firmware (ESP-IDF, Cheap Yellow Display).

The wire format (`docs/wire-format.md`) is the daemon ↔ firmware contract — agent-agnostic, hand-mirrored in both languages.

## Multi-agent architecture (client)

The Python daemon supports multiple upstream agents (Claude Code, Codex CLI, ...) running concurrently. The key modules:

- `client/src/burnscope_client/schema.py` — `SessionSnapshot` and `AgentSnapshot` (frozen dataclasses). Agent-agnostic; matches `docs/wire-format.md`.
- `client/src/burnscope_client/agent.py` — `Agent` ABC. Every supported provider is a subclass. Defines `name`, `probe()`, `load_credential()`, and a default `try_create()` for the CLI's auto-detect path.
- `client/src/burnscope_client/credentials.py` — `CredentialsError` plus the `Credential` ABC. The base class provides the shared *reading capabilities* (`_read_keychain`, `_read_file`, `_parse_json`) that every `*Credential` subclass composes in its own `load()`.
- `client/src/burnscope_client/agents/` — one module per provider. Each module owns:
  - a frozen `<Name>Credential(Credential)` dataclass with its `load()` classmethod,
  - a `<Name>Agent(Agent)` subclass whose class attributes name where the credential lives (`KEYCHAIN_SERVICE`, `CREDENTIALS_PATH`), and whose `probe()` normalises the upstream scale to `0.0`-`1.0`.
- `client/src/burnscope_client/daemon.py` — `DaemonConfig` takes `agents: list[Agent]`; the loop probes each agent on an independent cadence, isolates per-agent failures, and pushes one `AgentSnapshot` per agent per cycle.
- `client/src/burnscope_client/cli.py` — `--agent` flag (repeatable). When absent, every agent whose credentials load is included.

To add an agent: subclass `Credential` for the auth blob, subclass `Agent` for the probe, register it in `cli.py`'s `_AGENT_CLASSES`. See `agents/claude.py` and `agents/codex.py` for templates and the README's "Adding a new agent" section for the checklist.

## Firmware

The MVP is currently being developed on a Cheap-Yellow-Display (CYD), the board is a `cyd2usb` variant (one USB-C and one micro-USB port) -- ST7789, 320×240 landscape.

## ESP-IDF

Path: `~/.espressif/v6.0.1/esp-idf`

## Pointers

- Code style: [./rules/code-style.md](./rules/code-style.md)
- Project description: [../docs/description.md](../docs/description.md)
- Wire format: [../docs/wire-format.md](../docs/wire-format.md)
- Client spec (per-component): [../docs/client-spec.html](../docs/client-spec.html)
