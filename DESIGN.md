# Context Bridge — design

> One shared brain for many bots, with a git repository as the source of truth.

Context Bridge lets several independent agent instances — each with its own process, state
directory and chat identity — share what they know, and lets one person supervise all of them
from one place. It is built around a deliberately boring transport: **files in a git repo**.

## 1. Problem

A fleet of `dsh-superagent` bots on one host (e.g. `opus`, `fable`, `sonnet`) behaves like a team
of specialists who never talk:

- each bot's memory — tickets, decisions, reports, session transcripts — lives in its own
  private state directory and is invisible to the others;
- there is no shared task board, so hand-offs happen in chat messages that scroll away;
- a person must open a separate panel per bot to see progress, and cannot instruct one bot from
  a single console.

Agent Teams solve this **inside one process**. Across processes and profiles there is nothing.
Context Bridge is that missing layer.

## 2. Goals and non-goals

Goals

- **Readable by bots**: a bot can discover and query another bot's recent work from its shell,
  in one command, with no service to join and no credentials to hold.
- **Readable by people**: one web console showing fleet status, every bot's timeline, full-text
  memory search, a shared board, and a command console.
- **Cheap and durable**: the hub is a git repo. Snapshot, diff, restore and audit are `git`
  operations. No daemon to keep alive for durability, no DB server.
- **Safe to share**: the live hub holds internal material, so records are redacted **at write
  time**. The code repository is public; the data repository is local and private.
- **Boring to operate**: Python standard library only, one process, one port.

Non-goals

- Real-time message passing between bots. Chat sources (Feishu, GitHub) already do that; the
  bridge records and summarizes, it does not replace the chat transport.
- Replacing the bots' own state stores. The hub is a derivative, read-only projection plus a
  thin layer of bridge-authored notes and board cards.
- Hosting memory content outside the host by default.

## 3. Architecture

```
            ┌──────────────────── one host ────────────────────┐
 bot state  │  opus/      fable/      sonnet/                  │
 (sqlite +  │  orchestrator.sqlite    …                       │
  sessions) │      │          │          │                    │
            │      └──────────┼──────────┘                    │
            │            collector (ctxbridge sync)            │
            │                 │ redact → normalize             │
            │            ┌────▼─────────────┐                  │
            │            │  hub (git repo)  │  records/ digests/│
            │            │  local, private  │  board/ commands/ │
            │            └────┬─────────────┘  index/ (sqlite)  │
            │                 │                                │
            │       ┌─────────▼─────────┐     ┌──────────────┐ │
            │       │  ctxbridge CLI    │     │ web console  │ │
            │       │  search/recall/   │     │  :PORT       │ │
            │       │  note/board       │     │ (server.py)  │ │
            │       └─────────┬─────────┘     └──────┬───────┘ │
            └─────────────────┼──────────────────────┼─────────┘
                              │                      │
                    bots read/write memory     people read & instruct
                              │                      │
                              └──── chat source (Feishu) ◄── command relay
```

Components

| Component | Module | Responsibility |
| --- | --- | --- |
| Config | `config.py` | Parse `config.toml`: hub, server, bots, redaction, chat relay |
| Model | `model.py` | Record / BotStatus / BoardCard / Command dataclasses, ids, timestamps |
| Collectors | `collect/` | Read bot state (superagent sqlite, DSH session transcripts) → records |
| Redactor | `redact.py` | Mask secrets and internal endpoints before anything is written |
| Digest | `digest.py` | Deterministic per-bot daily digest Markdown from records |
| Hub | `hub.py` | Atomic writes, git commits, optional pull/push, file lock, reads |
| Index | `index.py` | SQLite FTS5 index over records (rebuildable cache) |
| Board | `board.py` | Shared kanban `board/board.json` with locked read-modify-write |
| Dispatch | `dispatch.py` | Relay a console command to a bot through its chat source; record it |
| Server | `server.py` | HTTP API + static web console + SSE change feed |
| CLI | `cli.py` | `init / gen-config / sync / search / recall / note / board / serve / status` |

## 4. The hub repository

The hub is an ordinary git repository, created by `ctxbridge init`. Layout:

```
hub/
├── manifest.json                 # schema version, bot registry, last sync info
├── config.toml                   # deployment config (gitignored, local only)
├── records/<bot>/<kind>/<id>.json
├── digests/<bot>/<YYYY-MM-DD>.md
├── board/board.json
├── commands/<command-id>.json
├── state/
│   ├── cursors.json              # per-source incremental cursors
│   └── last-sync.json
└── index/search.sqlite           # rebuildable FTS cache (gitignored)
```

Concurrency model

- Every record id is `"<bot>:<kind>:<source-id>"`, so a record file has exactly one writer
  (that bot's collector). Two bots never write the same path.
- Writes are atomic: write `<file>.tmp`, `os.replace`, then commit.
- Read-modify-write objects (`board.json`, `manifest.json`, `cursors.json`) are guarded by an
  `fcntl` lock on `state/lock`.
- Git commits are batched per sync run, authored as the bot when one bot ran, or as the
  collector when several did. `git pull --rebase` before push.

Sharing modes

| Mode | Hub remote | Use |
| --- | --- | --- |
| Local (default) | none | One host, fastest, no data leaves the machine |
| Private remote | any git URL | Multiple hosts; data stays in a private repo; credentials from the environment/credential helper, never in config |
| Public mirror | n/a | Not supported for live data. Only `examples/demo-hub` (synthetic) is published |

## 5. Record model

One JSON object per unit of memory. Versioned envelope (`v: 1`).

```json
{
  "v": 1,
  "id": "opus:ticket:9808e853a1dc52359a1fe5e9820fb132",
  "bot": "opus",
  "kind": "ticket",
  "title": "Build cross-bot context bridge",
  "summary": "Plain-text / Markdown summary, already redacted.",
  "created_at": "2026-11-13T19:49:00.000Z",
  "updated_at": "2026-11-13T21:02:00.000Z",
  "tags": ["bridge", "feishu"],
  "refs": {"ticket": "9808e853...", "url": "https://applink.feishu.cn/..."},
  "source": {"system": "dsh-superagent", "profile": "superagent", "path": "/srv/bots/opus/orchestrator.sqlite", "source_id": "9808e853..."},
  "payload": {"state": "open", "reports": [{"state": "progress", "summary": "..."}]},
  "redactions": 3
}
```

Kinds (extensible; unknown kinds are preserved and rendered generically):

| kind | source | meaning |
| --- | --- | --- |
| `ticket` | superagent `ticket` table | a unit of authorized work, with scope and latest reports |
| `decision` | superagent `decision` table | accept / ignore / stop / unavailable judgement with reason |
| `trigger` | superagent `trigger` table | a human message that reached a bot (redacted text) |
| `session` | DSH session transcripts | a digest of one agent session: title, first request, outcome |
| `note` | `ctxbridge note` | a memory note authored by a bot for others |
| `command` | web console | an instruction relayed to a bot, with delivery status |

Sessions are digested, never copied: the digest keeps the title, the originating request
(truncated), the final report text and tool activity counts. Full transcripts stay in the bot's
own store.

## 6. Redaction policy

Redaction runs on every string before it reaches the hub. Two layers:

1. **Never-collect**: the collector only reads structured state and session text. It never reads
   credential files (`.env`, `*.credentials*`, SSH keys, token stores) or repository secrets.
2. **Mask-at-write** (`redact.py`), default-on rules:
   - secret shapes: `sk-…`, `ghp_/gho_/github_pat_…`, `AKIA…`, `xox[baprs]-…`, JWTs, PEM private
     keys, `Bearer …`, password/api-key/secret/token assignments, `user:pass@` URLs;
   - internal endpoints: private IP ranges, corporate domains (blocklist in `scripts/check_public_safety.py`), loopback URLs
     with ports, internal hostnames;
   - long high-entropy strings in secret-looking contexts.
   - Git SHAs (40 hex), UUIDs and normal identifiers are preserved — they are not credentials.

Each record carries `redactions: N`, and the hub keeps `state/redaction.json` with per-rule
counts. The web console surfaces these counts so masking is visible, not silent. Tests assert on
a corpus of known secret shapes that nothing leaks.

## 7. Web console

Single-page console served by `server.py` on one port. Pages:

- **概览 (Overview)** — fleet status cards, records/attention counts, 14-day activity, recent
  events, redaction summary.
- **智能体 (Bots)** — per-bot detail: status, sync age, recent tickets/decisions/sessions,
  daily digests.
- **记忆 (Memory)** — unified timeline with bot/kind/date filters and full-text search.
- **看板 (Board)** — shared kanban; people and bots add/move cards.
- **控制台 (Console)** — pick a bot, type an instruction, send it through the bot's chat source;
  delivery status and resulting activity are shown.
- **关于 (About)** — hub path, schema, redaction rules, deployment notes.

Live updates use Server-Sent Events (`GET /api/events`); the server watches hub files and
pushes `sync` / `board` / `command` events. No build step: plain HTML/CSS/JS, no node_modules.

See [API.md](API.md) for the exact endpoint contract.

## 8. How a bot uses it

```bash
# What has sonnet been doing? (searches every bot, newest first)
ctxbridge search "context bridge" --bot all --limit 20

# Recent memory of one bot
ctxbridge recall opus --days 2

# Contribute a memory note other bots can find
ctxbridge note --bot sonnet "Found the bridge service on :8080; hub at /srv/context-hub" --tag infra

# Shared board
ctxbridge board list
ctxbridge board add "Review bridge PR" --column review --assignee opus
```

Bots learn these commands from the bundled DSH skill
(`integrations/dsh-skill/SKILL.md`), which is installed once into the shared skills directory.

The command console does not inject into a bot's process directly. It sends the instruction
through the bot's own chat source (a Feishu message that addresses the bot), so the bot's normal
intake, authorization and audit rules still apply. The bridge only records what it relayed.

## 9. Deployment (one host)

```
systemd/tmux:
  ctxbridge sync --loop 60     # collector daemon
  ctxbridge serve --port 8080  # web console (bind 0.0.0.0 behind the host firewall)
```

`scripts/deploy.sh` writes the deployment config, starts both in `tmux` sessions, waits for
health, and prints the console URL. `scripts/screenshot.py` captures the console with Playwright
for review evidence. Logs go to `<hub>/run/*.log`.

Security posture: the console binds to the host's NAT interface and has no default
authentication — deploy it only where the firewall already restricts access, or put it behind a
reverse proxy. The hub holds redacted but still internal material; treat it as private data.

## 10. Failure and evolution

- Collectors are incremental and idempotent: cursors live in `state/cursors.json`, records are
  rewritten only when their source row changed. A crash loses at most one sync cycle.
- The index is a cache; deleting it and running `ctxbridge index` rebuilds it.
- Unknown future fields are preserved in `payload` and ignored by old clients.
- Roadmap: embedding-based recall, cross-host hub with private remote, per-bot push webhooks,
  GitHub source adapter.
