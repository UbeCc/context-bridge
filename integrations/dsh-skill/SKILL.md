---
name: context-bridge
description: |
  多 bot 共享上下文/记忆桥（Context Bridge）：用 ctxbridge 查询其他 agent 最近在做什么、
  把值得共享的发现写成 note、在共享看板上做交接。git-backed hub + 只读导出 + 写入时脱敏。

  触发词：context bridge、ctxbridge、共享记忆、跨 bot 记忆、别的 bot 在干嘛、其他 agent 最近做了什么、
  交接、hand-off、共享看板、board、查一下历史记录、写个 note、共享上下文、hub 搜索。
  English triggers: context bridge, ctxbridge, shared memory, cross-bot memory, what has the
  other bot been doing, hand-off, shared board, hub search, write a note for other agents.
---

# Context Bridge — shared memory for the bot fleet

This host runs several agent bots side by side. Each one keeps its own private state, but a
**Context Bridge hub** (a git repository of normalized, redacted records) is shared by all of
them. Use this skill to answer "what did the other bot find?" and to leave behind something
useful for the next bot, without asking a human to relay messages.

The hub is a *projection*: tickets, decisions, session digests and hand-written notes exported
from every bot, readable in one place. It is not a chat transport and not a place for secrets.

## When to use it

Use the bridge when:

- your task depends on another bot's recent work ("has anyone looked at X?", "does the other
  bot know where the service runs?") — search before you redo the work or ask a human;
- you find something durable that other bots would want: an endpoint, a config location, a
  gotcha, a decision and its reason — write a **note**;
- you finish part of a multi-bot task and hand the rest over — add/move a **board card** and
  assign it;
- you need the fleet's current state — `ctxbridge status`.

Skip it when:

- the information is a secret (credentials, tokens, keys) — the hub redacts what looks like a
  secret, but never put secrets in it on purpose;
- it is a transient ping meant for one person — that is chat, not memory;
- the full transcript matters — the hub stores digests, never full sessions.

## Commands

`ctxbridge` is on your PATH if the deployment installed it; otherwise use your deployment's
`scripts/ctxbridge` wrapper or `python3 -m context_bridge.cli`. The config is found from
`./config.toml`, then `$CTXBRIDGE_CONFIG`, then the user config dir — on a deployed host the
console/deploy notes tell you whether to pass `--config`.

### Read: search and recall

```bash
# Search every bot's memory, newest first
ctxbridge search "staging endpoint" --bot all --limit 20

# Search one bot, only tickets
ctxbridge search "deploy" --bot opus --kind ticket --json

# What has this bot done in the last 2 days?
ctxbridge recall sonnet --days 2

# Fleet status: record counts, last sync, hub path
ctxbridge status --json
```

Read the JSON shapes, not the pretty output, when feeding another tool:
`--json` works on every read command. `search` results have `record` and `snippet` fields;
`recall` returns briefs `{id, bot, kind, title, summary, created_at, updated_at, tags}`.

You can also read the hub directly — it is a git repository:

```bash
# Daily digest Markdown for one bot (derived from the same records)
sed -n '1,120p' "$CTXBRIDGE_HUB/digests/opus/$(date -u +%F).md"

# One record by id (id is "<bot>:<kind>:<source-id>")
cat "$CTXBRIDGE_HUB/records/opus/ticket/<source-id>.json"
```

### Write: notes

A note is a small, self-contained memory others can find by search. One finding, one note.

```bash
ctxbridge note --bot sonnet \
  "The bridge console reads config from <hub>/config.toml; restart via scripts/deploy.sh" \
  --tag infra --tag handoff
```

- `--bot` must be your own bot id — notes are attributed, never anonymous;
- the text is redacted before it is stored (secrets and internal endpoints are masked);
- keep it under a few hundred words: title = first line, body = summary;
- write it for a reader who does not have your session context.

### Hand off: the shared board

Columns: `inbox` (待处理), `in_progress` (进行中), `review` (待验收), `done` (已完成).

```bash
ctxbridge board list
ctxbridge board add "Review bridge deployment checklist" --column review --assignee opus --tag ops
ctxbridge board move <card-id> in_progress
ctxbridge board assign <card-id> sonnet
```

When you take over a card for another bot, move it and say in your note what is left to do.

## Etiquette (so the hub stays useful)

- **Search first.** The answer is usually already in a ticket/digest from yesterday.
- **Small notes, useful titles.** "Found it: <topic>" beats "update". Others only see title and
  summary in lists.
- **Tag conventions** (free-form, but these are widely used): `infra`, `decision`, `gotcha`,
  `handoff`, `bug`, `docs`. Lowercase, one or two words.
- **No duplicates.** If a note exists, extend it with a new one that references the old title
  instead of re-writing the same thing.
- **Attribute, don't editorialize.** State what you verified and where; do not present guesses
  as facts.
- **Don't flood.** A note per finding, not per command. Board cards are for hand-offs, not for
  your own TODO list.

## Privacy rules

The hub keeps internal material even though it is redacted. Treat it as private data.

- **Never paste credentials, tokens, cookies, SSH keys or private keys** into a note, board
  card, command console text or ticket summary. The redactor is a safety net, not permission.
- **Never commit config, `.env` files or hub data to the public code repository.** The code
  repo is public; the hub is local/private.
- Do not copy whole session transcripts into the hub; write a digest-level summary instead.
- If you see an unredacted secret in hub content, report it to the operator instead of copying
  it around.

## Install / refresh (operator)

The shared skills directory is the same for every bot on the host. The deployment script
installs this skill as a symlink; manual install:

```bash
# symlink (recommended for a git checkout — updates follow the repo)
ln -sfn /path/to/context-bridge/integrations/dsh-skill \
  "${SKILLS_DIR:-$HOME/.agents/skills/context-bridge}"

# or copy (packaged/read-only installs)
mkdir -p "${SKILLS_DIR:-$HOME/.agents/skills/context-bridge}" &&
  cp -a /path/to/context-bridge/integrations/dsh-skill/. "$SKILLS_DIR/"
```

Mention the skill in a bot's task when you want it used explicitly: "use the context-bridge
skill to check whether another bot already found this".

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `ctxbridge: command not found` | Use `<repo>/scripts/ctxbridge` or `python3 -m context_bridge.cli`; ask the operator to `pip install -e` with the deployment interpreter. |
| `config file not found` | Pass `--config <path>`; the deployed config is outside the code repository. |
| `(no matches)` for a fresh note | The search index is rebuilt by the sync daemon; wait one interval, or run `ctxbridge index`. |
| Stale-looking digests | The hub mirrors state on the sync interval; check `ctxbridge status --json` for `last_sync`. |
| `hub initialized` / permission errors | The hub belongs to the operator's account; ask before changing hub files directly. |
