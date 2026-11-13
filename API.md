# Context Bridge HTTP API

All responses are JSON (`Content-Type: application/json; charset=utf-8`), except static files.
Timestamps are ISO-8601 UTC strings. Errors use HTTP status + `{"error": {"code": "...", "message": "..."}}`.
The server is same-origin: it serves the console and the API from one port, no CORS.

## Read endpoints

### `GET /api/health`
```json
{"ok": true, "version": "0.1.0", "uptime_s": 42,
 "hub": {"dir": "/srv/context-hub", "records": 1234, "bots": 3, "last_sync": "2026-11-13T21:02:00Z"},
 "redaction": {"total": 217, "by_rule": {"api_key": 12, "internal_host": 205}}}
```

### `GET /api/overview`
```json
{"generated_at": "...",
 "bots": [{"id": "opus", "label": "Opus", "profile": "superagent", "color": "#7c5cff",
           "status": "active", "last_sync": "...", "counts": {"ticket": 79, "session": 122},
           "open_tickets": 3, "attention": 1}],
 "counts": {"records": 1234, "by_kind": {"ticket": 88, "session": 300}, "attention": 2},
 "activity": [{"date": "2026-11-13", "count": 42}],
 "recent": [RecordBrief],
 "attention": [{"bot": "opus", "record_id": "...", "title": "...", "question": "...", "at": "..."}]}
```
`RecordBrief` = `{id, bot, kind, title, summary, created_at, updated_at, tags}`.

### `GET /api/bots`
```json
{"items": [BotSummary]}
```
### `GET /api/bots/{id}`
```json
{"bot": BotSummary, "recent": [RecordBrief], "digests": [{"date": "2026-11-13", "path": "digests/opus/2026-11-13.md"}]}
```
### `GET /api/records?bot=&kind=&q=&limit=50&offset=0`
```json
{"items": [RecordBrief], "total": 123, "limit": 50, "offset": 0}
```
### `GET /api/records/{id}`
Full `Record` (see DESIGN.md §5). `404` if missing. The `id` contains `:` and is URL-encoded.
### `GET /api/search?q=&bot=&kind=&limit=30`
```json
{"items": [{"record": RecordBrief, "score": 1.0, "snippet": "…marked <b>term</b>…"}], "query": "..."}
```
### `GET /api/board`
```json
{"columns": [{"id": "inbox", "title": "待处理"}, {"id": "in_progress", "title": "进行中"},
             {"id": "review", "title": "待验收"}, {"id": "done", "title": "已完成"}],
 "cards": [{"id": "c_ab12", "title": "...", "desc": "...", "column": "inbox",
            "assignee": "sonnet", "tags": ["bridge"], "created_at": "...", "updated_at": "...", "author": "human"}]}
```
### `GET /api/commands?limit=30`
```json
{"items": [{"id": "cmd_...", "target_bot": "sonnet", "text": "...", "via": "feishu",
            "status": "sent", "message_id": "om_...", "error": null, "created_at": "..."}]}
```
### `GET /api/events` (Server-Sent Events)
Events: `hello` (`{"ok":true}`), `sync` (`{"at":"...","records":42}`), `board` (`{"at":"..."}`),
`command` (`{"id":"cmd_..."}`). Comment heartbeat every 15 s.

## Write endpoints

### `POST /api/board/cards`
Body `{"title": "...", "desc": "", "column": "inbox", "assignee": null, "tags": [], "author": "human"}`
→ `201 {"card": BoardCard}`. Unknown column → `400`.

### `PATCH /api/board/cards/{id}`
Body with any of `title`, `desc`, `column`, `assignee`, `tags` → `200 {"card": BoardCard}`.
### `DELETE /api/board/cards/{id}` → `200 {"ok": true}`

### `POST /api/commands`
Body `{"target_bot": "sonnet", "text": "check the bridge hub and summarize opus memory"}` →
```json
{"command": {"id": "cmd_...", "status": "sent", "message_id": "om_...", "target_bot": "sonnet", "text": "...", "created_at": "..."}}
```
Notes: the server relays the text through the configured chat source (Feishu) as the bridge bot,
addressing the target bot. `status` is `sent`, `failed` or `skipped` (relay disabled). `502` if
the chat CLI fails; the command record is still written with `error`.

### `POST /api/sync` (optional manual trigger)
Body `{}` → `202 {"ok": true}`; runs one collector pass in the background.

## Static content
`GET /` → `web/index.html`
`GET /<asset>` → file under `web/` (content types by extension, path traversal rejected)
Any unknown `/api/*` → `404`; any unknown non-API path → `index.html` (SPA fallback).
