# BurnScope

> An always-on token & quota meter for your AI coding agents.

BurnScope tracks rate-limit usage for your AI coding agents and surfaces it on a dedicated ESP32 desk display. A small daemon on your laptop probes each agent's rate-limit headers, computes the current window summary, discovers the ESP32 over mDNS, and pushes the rendered numbers straight to it over HTTP. No intermediate server.

When you hit the cap and walk away, an always-on display tells you "how much have I used / when does it reset" without unlocking a laptop.

## Supported agents

| Agent | Storage | Windows |
| --- | --- | --- |
| **Claude Code** | macOS keychain (`Claude Code-credentials`) or `~/.claude/.credentials.json` | `5h`, `7d` |
| **Codex CLI** | `~/.codex/auth.json` (access token + optional ChatGPT account id) | `primary`, `secondary` |

The daemon auto-detects which agents are logged in and runs all of them concurrently — one `AgentSnapshot` per agent per cycle. Restrict to a subset with `--agent`.

## Architecture

```
┌──────────────────────────┐   HTTP POST /summary    ┌─────────────────────┐
│ Python daemon (laptop)   │ ──────────────────────▶ │  ESP32 (CYD)        │
│ - probes each agent      │   AgentSnapshot JSON    │  - advertises mDNS  │
│ - tails activity         │                         │  - tiny HTTP server │
│ - discovers ESP32 (mDNS) │                         │  - renders TFT      │
└──────────────────────────┘                         └─────────────────────┘
```

See [`docs/description.md`](./docs/description.md) for the design rationale, [`docs/wire-format.md`](./docs/wire-format.md) for the daemon ↔ firmware contract, and [`docs/client-spec.html`](./docs/client-spec.html) for a component-level specification of the Python daemon.

## Running the daemon

The CLI entry point is `burnscope-client`. With both Claude and Codex logged in:

```bash
burnscope-client                       # probe every detected agent
burnscope-client --agent claude        # restrict to one agent
burnscope-client --agent claude --agent codex   # explicit list
burnscope-client --esp32-host burnscope.local   # bypass mDNS
```

The daemon exits with a friendly message if no agent credentials are found.

## Repository layout

```
burnscope/
├── README.md
├── CLAUDE.md            ← in-repo agent instructions
├── docs/
│   ├── description.md   ← project goals, scope, design notes
│   └── wire-format.md   ← daemon ↔ firmware contract
├── client/              ← Python daemon
│   └── src/burnscope_client/
│       ├── schema.py        ← wire-format dataclasses
│       ├── agent.py         ← Agent ABC
│       ├── credentials.py   ← Credential ABC + readers
│       ├── agents/          ← one module per supported provider
│       ├── daemon.py
│       └── cli.py
└── firmware/            ← ESP32 firmware (PlatformIO, CYD)
```

## Adding a new agent

1. Create `client/src/burnscope_client/agents/<name>.py`.
2. Define a frozen `<Name>Credential(Credential)` dataclass with a `load(**kwargs)` classmethod — reuse `cls._read_file`, `cls._read_keychain`, and `cls._parse_json` from the base.
3. Define `<Name>Agent(Agent)` with `name`, the credential storage location as class attributes, an `__init__(credential)`, a `probe()` that returns an `AgentSnapshot` (normalising any upstream scale to `0.0`-`1.0`), and a `load_credential()` classmethod.
4. Register the class in `_AGENT_CLASSES` in `cli.py`.

See `agents/claude.py` and `agents/codex.py` for working examples.
