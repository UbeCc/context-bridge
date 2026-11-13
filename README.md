# Context Bridge

> Shared context and memory hub for multiple AI agent bots — a git-backed memory bus with a web console.

Context Bridge connects independent agent instances (for example several DeepSeek Harness `dsh-superagent` bots) so that each bot can read what the others have been doing, and one person can supervise all of them from a single dashboard.

- **Git-backed context hub** — every bot's tickets, decisions, sessions and notes are exported as normalized, redacted JSON/Markdown records into a local git repository. No database server, no message broker.
- **Cross-bot memory** — `ctxbridge search`, `ctxbridge recall` and `ctxbridge note` let any bot query or contribute to the shared memory from its normal shell.
- **Shared board** — a kanban board in the hub for tasks, decisions and hand-offs that all bots and people can read and update.
- **Web console** — overview, per-bot timelines, full-text memory search, board, and a command console that relays instructions to a bot through its chat source.
- **Redaction first** — credentials and internal endpoints are masked at write time; the public repository contains only the generic implementation, never the live data.

Status: research-grade tooling. It runs as a local service on an agent host; the live hub data stays private.

## Quick start

```bash
# 1. Install (Python 3.11+)
python3 -m pip install -e .

# 2. Create a hub and a deployment config
ctxbridge init --dir ~/context-hub
ctxbridge gen-config --bots-file bots.example.toml > config.toml

# 3. Export bot memory into the hub (git commits each batch)
ctxbridge sync

# 4. Serve the web console
ctxbridge serve --port 8080
```

See [DESIGN.md](DESIGN.md) for the architecture, [API.md](API.md) for the HTTP API and [AGENTS.md](AGENTS.md) for repository conventions.

## License

MIT
