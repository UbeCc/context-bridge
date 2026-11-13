# Context Bridge web console

A single-page console for the shared context hub. Plain HTML/CSS/JS — **no build step, no
`node_modules`, no CDN and no external network resources** — so it runs on an offline host and is
served straight from `web/` by `ctxbridge serve` (`context_bridge/server.py`) on the same origin as
the API.

```
web/
├── index.html          # app shell: inline SVG icon sprite, favicon, nav, mounts
├── styles.css          # dark (default) + light theme, responsive, motion, no external fonts
├── app.js              # views, router, API client, SSE, drawers/modals/toasts, Markdown renderer
├── dev/
│   ├── mock-api.py     # stdlib mock of API.md with fictional demo data (development fallback)
│   └── capture-screenshots.py  # Playwright capture of the committed PR screenshots
└── assets/             # reserved for local static assets (currently icons are inline SVG)
```

The console is built strictly against [`API.md`](../API.md). It only talks to relative URLs
(`fetch("/api/...")`, `EventSource("/api/events")`), so it works behind any host or reverse proxy.

## Running it

### Against the real server (recommended)

The repository has no live data; for demos and screenshots generate the synthetic hub:

```bash
python3 scripts/make-demo-hub.py --out /tmp/demo-hub
python3 -m context_bridge.cli --config /tmp/demo-hub/demo.toml serve --port 8080
# → http://localhost:8080/
```

Or point it at your own deployment config:

```bash
ctxbridge --config /path/to/config.toml serve --port 8080
```

### Against the mock API (no hub needed)

`web/dev/mock-api.py` serves API.md-shaped **fictional** data plus these static files, so the UI
can be developed, reviewed and screenshotted without a hub, without bot state and without Feishu:

```bash
python3 web/dev/mock-api.py                 # http://localhost:8787/
python3 web/dev/mock-api.py --port 9000
python3 web/dev/mock-api.py --no-fake-events  # stable page, no periodic SSE events
python3 web/dev/mock-api.py --token secret    # exercise the 401 → set-token flow
```

The mock implements every endpoint in `API.md`, including board write endpoints, command
recording (fictional `status: sent`), `POST /api/sync` and SSE (`hello`, `sync`, `board`,
`command`). Its data is entirely made up and safe to publish; it is never a substitute for the
real server.

## Views

| View | What it does |
| --- | --- |
| **概览 Overview** | Fleet cards (status, color, per-bot counts, open tickets, attention), key metrics, 14-day activity chart (SVG), recent activity feed, attention items, redaction summary. |
| **智能体 Bots** | Fleet list plus per-bot detail: status/sync age, memory timeline grouped by day, daily digest viewer (server Markdown, with a client-side fallback that composes a day summary from records if the digest is missing). |
| **记忆 Memory** | Unified cross-bot timeline with bot/kind/date filters, debounced full-text search against `/api/search` (highlighted snippets), pagination, and a record detail drawer showing the full payload, refs, source and redaction count. |
| **看板 Board** | Shared kanban for the four API.md columns: create, edit, delete, drag-and-drop and keyboard-accessible move buttons, assignee picker, tags. Mutations are optimistic with rollback. |
| **控制台 Console** | Pick a bot, write an instruction, `POST /api/commands`. Shows delivery status (`sent` / `skipped` dry-run / `failed`), message id or error, and command history. The relay goes through the chat source; the console never injects into a bot process. |
| **关于 About** | Hub/version/uptime, memory schema and kinds, redaction rules with per-rule counts, API endpoint table, deployment quickstart, keyboard shortcuts and the optional access-token manager. |

Cross-cutting behaviour:

- **Live updates** via SSE with automatic reconnect/backoff; the sidebar shows connection state.
  `sync`, `board` and `command` events refresh the relevant view quietly.
- **Loading / empty / error states** everywhere; failed requests offer a retry.
- **Toasts** for actions (create/move/delete card, command delivery, sync, token changes).
- **Theme**: dark by default, light toggle persisted in `localStorage`; respects
  `prefers-reduced-motion`.
- **Access tokens**: if the server has `server.token` set, the console stores a token in
  `localStorage` (About → 访问令牌, or the automatic 401 prompt) and sends it as
  `Authorization: Bearer …`. The SSE URL also carries `?token=`.
- **Accessibility**: keyboard navigation, focus-visible rings, focus trapping in drawers/modals,
  `Esc` closes overlays, `/` or `Ctrl/Cmd+K` focuses memory search, `T` toggles the theme,
  `Ctrl/Cmd+Enter` sends a console command.
- **No emoji font required**: bot avatars use the configured emoji only when the host actually has
  a colour emoji font (detected with a canvas probe) and fall back to a colour-styled initial.

## Screenshots

Committed evidence for the PR (all from the **synthetic demo hub**, never live data):

| File | Viewport | Represents |
| --- | --- | --- |
| `docs/screenshots/before.png` | 1440×900 | first functional, deliberately plain layout |
| `docs/screenshots/after.png` | 1440×900 | final polished dark UI (overview) |
| `docs/screenshots/after-mobile.png` | 390×844 | final responsive layout |

Reproduce with:

```bash
scripts/... # start the demo hub server on :18098 (see above)
python3 web/dev/capture-screenshots.py --base http://localhost:18098
```

The capture script verifies there are no console errors, checks the mobile viewport for horizontal
overflow, exercises board create/move/delete, memory search + record drawer and the console
dry-run state, then writes the screenshots. Images are kept small enough for the repository.

## Notes for implementers

- Everything is rendered from API.md data; unknown record kinds are shown generically and unknown
  fields are ignored, so the console tolerates forward-compatible hubs.
- Dynamic values are escaped before insertion (the only intentionally trusted HTML is the `<b>`
  marker in search snippets and a small client-side Markdown renderer that escapes first).
- `body[data-page]` reflects the current route; clicks are handled by one delegated listener.
- The console does not poll: SSE drives refreshes, with a slow hourly shell refresh as a fallback.
