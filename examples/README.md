# Synthetic demo hub

The console screenshots and the public demo use **generated, fictional data** — never a live
hub. The data is produced by [`scripts/make-demo-hub.py`](../scripts/make-demo-hub.py) and is
intentionally not committed (it is generated into a gitignored directory such as `demo-hub/`).

## Generate and serve

```bash
python3 scripts/make-demo-hub.py            # -> ./demo-hub (gitignored)
python3 -m context_bridge.cli --config ./demo-hub/demo.toml serve
# open http://localhost:8080/
```

Options:

```bash
python3 scripts/make-demo-hub.py --out /tmp/demo --force
python3 scripts/make-demo-hub.py --now 2026-11-13T12:00:00Z   # reproducible timestamps
python3 scripts/make-demo-hub.py --no-index --no-commit       # files only
```

## What is inside

The generated directory is a complete hub layout:

```
demo-hub/
├── demo.toml              # ready-to-serve config (fictional bots, localhost)
├── states/<bot>/          # empty placeholder state dirs
└── hub/                   # the demo hub repository
    ├── records/<bot>/<kind>/*.json   # tickets, decisions, triggers, sessions, notes
    ├── digests/<bot>/<date>.md       # three days of daily digests
    ├── board/board.json              # kanban with cards in every column
    ├── manifest.json
    └── state/{cursors,last-sync,redaction}.json
```

Three fictional bots (**Atlas**, **Beacon**, **Cinder**) collaborate on a fictional release:
docs, infrastructure and quality work respectively. Every record, name, path and chat id is
invented; the timestamps are relative to generation time so the 14-day activity chart is
populated.

## Screenshots

The images referenced from the top-level README (`docs/screenshots/after.png`,
`docs/screenshots/after-mobile.png`) are captured against exactly this dataset, so they can be
reproduced without exposing any private memory.
