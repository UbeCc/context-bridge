#!/usr/bin/env python3
"""Generate a synthetic, fictional Context Bridge hub for demos and screenshots.

The generated hub is a real hub layout (records / digests / board / manifest / state) filled
with invented bots and invented work, so the console can be demonstrated and screenshotted
without ever touching live agent data. Output is deterministic for a given ``--now``.

    python3 scripts/make-demo-hub.py                 # -> ./demo-hub
    python3 scripts/make-demo-hub.py --out /tmp/demo --force
    python3 scripts/make-demo-hub.py --now 2026-11-13T12:00:00Z

Then serve it:

    python3 -m context_bridge.cli --config ./demo-hub/demo.toml serve
    # open http://localhost:8080/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from context_bridge.hub import Hub  # noqa: E402
from context_bridge.model import BOARD_COLUMNS, BoardCard, Record  # noqa: E402

# ---------------------------------------------------------------------------------------------
# fictional fleet
# ---------------------------------------------------------------------------------------------
BOTS = [
    {"id": "atlas", "label": "Atlas", "profile": "superagent", "color": "#7c5cff", "emoji": "🟣"},
    {"id": "beacon", "label": "Beacon", "profile": "superagent-beacon", "color": "#2fc8b5", "emoji": "🩵"},
    {"id": "cinder", "label": "Cinder", "profile": "superagent-cinder", "color": "#f0a13b", "emoji": "🟠"},
]
BOT_IDS = [b["id"] for b in BOTS]

# ---------------------------------------------------------------------------------------------
# fictional activity: one entry per record, dated relative to --now
# ---------------------------------------------------------------------------------------------
TICKETS = {
    "atlas": [
        {
            "slug": "quickstart-v2",
            "title": "Rewrite the quickstart around the v2 console",
            "state": "ended",
            "days_ago": 9,
            "tags": ["docs", "release"],
            "summary": "Quickstart now follows one path: install, create a hub from a synthetic "
            "dataset, open the console. Removed the per-role branches that had drifted.",
            "reports": [
                {"state": "progress", "summary": "Drafted the new outline, dropping two stale sections."},
                {"state": "done", "summary": "Shipped; every command in the quickstart was re-run on a clean hub."},
            ],
        },
        {
            "slug": "redaction-doc",
            "title": "Document redaction rules for bot authors",
            "state": "open",
            "days_ago": 4,
            "tags": ["docs", "security"],
            "summary": "Writing a short page on what the redactor masks (secret shapes, private IPs, "
            "internal domains) and what it deliberately keeps (git SHAs, UUIDs, chat ids).",
            "reports": [
                {"state": "attention", "summary": "How explicit should the public page be about the "
                 "exact regex rules? Detailed rules help bot authors, but are also a map for anyone "
                 "probing the boundary."},
            ],
            "attention": "How explicit should the public page be about the exact masking rules?",
        },
        {
            "slug": "cli-examples",
            "title": "Fix stale CLI examples in the integration docs",
            "state": "ended",
            "days_ago": 6,
            "tags": ["docs"],
            "summary": "Rewrote examples to use --json output and board card ids; verified against "
            "the current parser.",
            "reports": [{"state": "done", "summary": "All examples checked with the CLI."}],
        },
        {
            "slug": "migration-guide",
            "title": "Draft the upgrade guide for hub schema v1",
            "state": "open",
            "days_ago": 1,
            "tags": ["docs", "schema"],
            "summary": "Collecting the differences between the prototype layout and the published "
            "schema; needs a final read from the hub owner before publishing.",
            "reports": [{"state": "progress", "summary": "Outline done, waiting on two decisions about id formats."}],
        },
    ],
    "beacon": [
        {
            "slug": "persistent-volume",
            "title": "Move the hub to the persistent volume",
            "state": "open",
            "days_ago": 3,
            "tags": ["infra", "handoff"],
            "summary": "Hub data must survive instance restarts. Mapping the mount, then copying the "
            "existing hub with git history intact and switching the deployment config.",
            "reports": [{"state": "progress", "summary": "Mount verified; dry-run copy takes about 40s."}],
        },
        {
            "slug": "deploy-health",
            "title": "Add health checks to the deploy script",
            "state": "ended",
            "days_ago": 8,
            "tags": ["infra", "deploy"],
            "summary": "The script now waits for /api/health, tails the server log on failure and "
            "leaves both tmux sessions running for inspection.",
            "reports": [{"state": "done", "summary": "Verified on a throwaway port; failure path shows the log tail."}],
        },
        {
            "slug": "relay-rotation",
            "title": "Rotate the chat relay credentials",
            "state": "open",
            "days_ago": 2,
            "tags": ["infra", "security"],
            "summary": "Preparing the replacement relay identity in the chat CLI's own login store. "
            "Nothing is stored in the repository or the hub config.",
            "reports": [
                {"state": "attention", "summary": "Need the operator to approve the replacement relay "
                 "identity before the switch; after the switch old bridge messages will not be sent."},
            ],
            "attention": "Operator approval needed before switching the relay identity.",
        },
        {
            "slug": "sync-interval",
            "title": "Tune the collector sync interval",
            "state": "ended",
            "days_ago": 11,
            "tags": ["infra", "tuning"],
            "summary": "Measured one collector pass on three bots: cheap enough for 60s. Settled on "
            "60s with sessions digested incrementally.",
            "reports": [{"state": "done", "summary": "60s interval adopted; CPU cost is negligible."}],
        },
    ],
    "cinder": [
        {
            "slug": "flaky-screenshots",
            "title": "Triage the flaky screenshot test",
            "state": "open",
            "days_ago": 5,
            "tags": ["quality", "bug"],
            "summary": "The mobile layout test fails only at a 390px viewport and only on the board "
            "view. Narrowing it to a timing issue in the drag-and-drop animation.",
            "reports": [
                {"state": "attention", "summary": "Is the 390px viewport a supported width for the "
                 "board view? If yes I will fix the animation; if not I will drop the assertion."},
            ],
            "attention": "Is the 390px viewport a supported width for the board view?",
        },
        {
            "slug": "board-smoke",
            "title": "Smoke-test the board API",
            "state": "ended",
            "days_ago": 7,
            "tags": ["quality", "api"],
            "summary": "Exercised create / move / assign / delete on a temp hub, including the "
            "unknown-column 400 path and the conflict on concurrent moves.",
            "reports": [{"state": "done", "summary": "All board endpoints behave as documented."}],
        },
        {
            "slug": "sse-reconnect",
            "title": "Verify SSE reconnect behaviour",
            "state": "ended",
            "days_ago": 10,
            "tags": ["quality"],
            "summary": "Dropped the connection mid-stream and confirmed the console reconnects and "
            "replays no stale events; heartbeat keeps proxies from closing the stream.",
            "reports": [{"state": "done", "summary": "Reconnect and heartbeat verified."}],
        },
        {
            "slug": "a11y-pass",
            "title": "Accessibility pass on the console",
            "state": "open",
            "days_ago": 0,
            "tags": ["quality", "web"],
            "summary": "Keyboard pass done for the board and console views; remaining items are "
            "focus order in the record drawer and contrast on the muted text.",
            "reports": [{"state": "progress", "summary": "Board and console are keyboard-navigable; drawer next."}],
        },
    ],
}

DECISIONS = {
    "atlas": [
        {
            "slug": "demo-dataset",
            "title": "Publish the demo as a separate synthetic dataset",
            "kind": "accept",
            "days_ago": 9,
            "tags": ["docs", "decision"],
            "reason": [
                "Screenshots and demos need realistic data.",
                "Live hub data must never leave the host, even redacted.",
                "A generated dataset can be reproduced in CI.",
            ],
        },
        {
            "slug": "docs-two-languages",
            "title": "Defer a two-language documentation rewrite",
            "kind": "ignore",
            "days_ago": 5,
            "tags": ["docs", "decision"],
            "reason": ["Scope: the console ships Chinese-first; a full English rewrite can follow."],
        },
        {
            "slug": "one-quickstart",
            "title": "Keep one quickstart instead of per-role guides",
            "kind": "accept",
            "days_ago": 2,
            "tags": ["docs", "decision"],
            "reason": ["Per-role guides had already drifted from the real commands."],
        },
    ],
    "beacon": [
        {
            "slug": "tmux-supervision",
            "title": "Supervise services with tmux, not a custom supervisor",
            "kind": "accept",
            "days_ago": 8,
            "tags": ["infra", "decision"],
            "reason": [
                "The host already uses tmux for every long-running service.",
                "Two sessions are easy to inspect and restart; no extra dependency.",
            ],
        },
        {
            "slug": "per-bot-daemons",
            "title": "Run one collector daemon for the whole fleet",
            "kind": "ignore",
            "days_ago": 6,
            "tags": ["infra", "decision"],
            "reason": ["One daemon already isolates failures per bot; per-bot daemons add more state to watch."],
        },
        {
            "slug": "rebuildable-index",
            "title": "Treat the search index as a rebuildable cache",
            "kind": "accept",
            "days_ago": 4,
            "tags": ["infra", "decision"],
            "reason": ["A corrupt index must never block a sync; rebuilding is one command."],
        },
    ],
    "cinder": [
        {
            "slug": "flake-policy",
            "title": "Treat a flake as blocking only after three reproductions",
            "kind": "accept",
            "days_ago": 7,
            "tags": ["quality", "decision"],
            "reason": ["Single reproductions were mostly environment noise; the rule keeps signal high."],
        },
        {
            "slug": "drop-mobile-check",
            "title": "Do not drop the mobile viewport assertion",
            "kind": "stop",
            "days_ago": 5,
            "tags": ["quality", "decision"],
            "reason": ["Mobile width is a supported form factor; removing the check would hide regressions."],
        },
        {
            "slug": "synthetic-screenshots",
            "title": "Use the synthetic demo hub for all public screenshots",
            "kind": "accept",
            "days_ago": 3,
            "tags": ["quality", "decision"],
            "reason": ["No live data in public artifacts, and screenshots become reproducible."],
        },
    ],
}

TRIGGERS = {
    "atlas": [
        {"days_ago": 9, "actor": "operator", "text": "The quickstart still points at the old console; please rewrite it end to end and re-run every command."},
        {"days_ago": 4, "actor": "operator", "text": "Write a short page explaining what the redactor masks — bot authors keep asking."},
        {"days_ago": 3, "actor": "atlas", "text": "Cross-check the CLI examples against the parser before the release notes."},
        {"days_ago": 1, "actor": "operator", "text": "Start the schema v1 upgrade guide, but wait for the hub owner before publishing it."},
        {"days_ago": 0, "actor": "operator", "text": "Once the quickstart is stable, hand the announcement draft to the board."},
    ],
    "beacon": [
        {"days_ago": 11, "actor": "operator", "text": "Measure how expensive one sync pass is; 60 seconds would be ideal."},
        {"days_ago": 8, "actor": "operator", "text": "The deploy script should fail loudly and leave logs when the console does not come up."},
        {"days_ago": 3, "actor": "operator", "text": "Move the hub data to the persistent volume without losing git history."},
        {"days_ago": 2, "actor": "operator", "text": "Plan the relay credential rotation; nothing in the repo, everything in the CLI login."},
        {"days_ago": 0, "actor": "beacon", "text": "Report the volume move status on the shared board when the copy is verified."},
    ],
    "cinder": [
        {"days_ago": 10, "actor": "operator", "text": "Check that the live event stream reconnects cleanly after a dropped connection."},
        {"days_ago": 7, "actor": "operator", "text": "Smoke-test every board endpoint on a throwaway hub before the demo."},
        {"days_ago": 5, "actor": "operator", "text": "The screenshot test flakes on mobile; find out whether it is the animation or the viewport."},
        {"days_ago": 2, "actor": "operator", "text": "Do a keyboard-only pass on the console and tell me what breaks."},
        {"days_ago": 0, "actor": "operator", "text": "Capture the before/after screenshots from the synthetic dataset, not a live hub."},
    ],
}

SESSIONS = {
    "atlas": [
        {"days_ago": 9, "hours": 3, "title": "Quickstart rewrite: outline and first draft", "first_request": "Rewrite the quickstart around the v2 console", "outcome": "New outline agreed; two stale sections removed; first draft committed.", "tools": {"read": 14, "edit": 6, "shell": 9}},
        {"days_ago": 8, "hours": 2, "title": "Re-run every quickstart command on a clean hub", "first_request": "Verify the quickstart commands actually work", "outcome": "Three broken commands fixed; fresh-hub install path now works offline.", "tools": {"shell": 22, "read": 8, "edit": 3}},
        {"days_ago": 6, "hours": 1, "title": "Sweep CLI examples in integration docs", "first_request": "Fix stale CLI examples", "outcome": "Examples updated to use --json and card ids.", "tools": {"read": 11, "edit": 9, "shell": 4}},
        {"days_ago": 4, "hours": 4, "title": "Redaction page: first draft", "first_request": "Explain what the redactor masks", "outcome": "Drafted the masking page; flagged an open question about rule detail.", "tools": {"read": 7, "edit": 5, "shell": 2}},
        {"days_ago": 2, "hours": 2, "title": "Compare schema variants for the upgrade guide", "first_request": "Draft the upgrade guide", "outcome": "Differences catalogued; two id-format questions remain open.", "tools": {"read": 15, "edit": 4, "shell": 3}},
        {"days_ago": 1, "hours": 5, "title": "Announcement draft from release notes", "first_request": "Draft the release announcement", "outcome": "Announcement outline ready; waiting for the quickstart to freeze.", "tools": {"read": 9, "edit": 7, "shell": 1}},
        {"days_ago": 0, "hours": 2, "title": "Cross-check docs against the live API", "first_request": "Check the documented endpoints against the server", "outcome": "Found one undocumented digest endpoint; noted it for the API owner.", "tools": {"read": 18, "edit": 2, "shell": 6}},
    ],
    "beacon": [
        {"days_ago": 11, "hours": 2, "title": "Profile one collector pass", "first_request": "Measure the cost of a sync pass", "outcome": "One pass over three bots is cheap; 60s interval is safe.", "tools": {"shell": 12, "read": 5}},
        {"days_ago": 8, "hours": 3, "title": "Deploy script health check", "first_request": "Make the deploy script fail loudly", "outcome": "Health wait, log tail and non-zero exit added and tested.", "tools": {"read": 10, "edit": 8, "shell": 15}},
        {"days_ago": 7, "hours": 1, "title": "Compare persistence options for the hub", "first_request": "Where should hub data live?", "outcome": "Persistent volume selected; copy plan drafted.", "tools": {"read": 6, "shell": 4}},
        {"days_ago": 3, "hours": 4, "title": "Dry-run the hub copy to the volume", "first_request": "Move the hub to the persistent volume", "outcome": "Dry run takes about 40s; git history preserved in the copy.", "tools": {"shell": 18, "read": 7, "edit": 2}},
        {"days_ago": 2, "hours": 2, "title": "Relay identity rotation plan", "first_request": "Plan the relay credential rotation", "outcome": "Plan written in a ticket; needs operator approval.", "tools": {"read": 8, "shell": 5}},
        {"days_ago": 1, "hours": 1, "title": "Check tmux session recovery after reboot", "first_request": "Will the services survive a restart?", "outcome": "They do not; documented the restart command in the runbook.", "tools": {"shell": 9, "read": 3}},
        {"days_ago": 0, "hours": 3, "title": "Verify the volume mount and free space", "first_request": "Confirm the volume is mounted and has room", "outcome": "Mount healthy, plenty of space; waiting for the copy window.", "tools": {"shell": 11, "read": 4}},
    ],
    "cinder": [
        {"days_ago": 10, "hours": 2, "title": "SSE reconnect test", "first_request": "Verify SSE reconnect behaviour", "outcome": "Reconnect works; heartbeat prevents proxy idle timeouts.", "tools": {"read": 6, "shell": 8}},
        {"days_ago": 7, "hours": 3, "title": "Board API smoke tests", "first_request": "Smoke-test the board endpoints", "outcome": "All endpoints pass, including error paths.", "tools": {"read": 9, "edit": 5, "shell": 12}},
        {"days_ago": 5, "hours": 4, "title": "Bisect the flaky screenshot test", "first_request": "Find why the mobile test flakes", "outcome": "Narrowed to board drag animation timing; question posted to the operator.", "tools": {"read": 13, "shell": 16, "edit": 3}},
        {"days_ago": 4, "hours": 2, "title": "Keyboard-only pass: board view", "first_request": "Check keyboard navigation", "outcome": "Card move by keyboard works; focus ring needs a contrast fix.", "tools": {"read": 5, "edit": 4}},
        {"days_ago": 2, "hours": 3, "title": "Contrast audit of the dark theme", "first_request": "Audit contrast", "outcome": "Two muted text tokens below AA; proposed replacements.", "tools": {"read": 8, "edit": 6}},
        {"days_ago": 1, "hours": 2, "title": "Keyboard pass: console view", "first_request": "Finish the keyboard pass", "outcome": "Console send flow keyboard-navigable; pending drawer focus order.", "tools": {"read": 7, "edit": 5}},
        {"days_ago": 0, "hours": 1, "title": "Record the demo screenshot procedure", "first_request": "Write down how to capture the screenshots", "outcome": "Procedure uses the synthetic hub only; three viewports covered.", "tools": {"read": 4, "edit": 3}},
    ],
}

NOTES = {
    "atlas": [
        {
            "days_ago": 8,
            "tags": ["docs", "handoff"],
            "title": "Quickstart is verified offline; don't add network steps",
            "summary": "Every command in the quickstart was re-run on a clean, offline hub. If you add "
            "a step that downloads anything, mark it optional or the offline path breaks.",
        },
        {
            "days_ago": 4,
            "tags": ["docs", "redaction"],
            "title": "Redaction page: open question on rule detail",
            "summary": "Masking rules page is drafted. Open question for the operator: how literal "
            "should the public description of the patterns be? See the redaction ticket.",
        },
        {
            "days_ago": 1,
            "tags": ["schema", "gotcha"],
            "title": "Record ids keep ':' — URL-encode before calling the API",
            "summary": "Record ids are '<bot>:<kind>:<source-id>'. The single-record endpoint needs the "
            "id percent-encoded; the console does this, shell users must too.",
        },
    ],
    "beacon": [
        {
            "days_ago": 8,
            "tags": ["infra", "deploy"],
            "title": "Deploy logs live under the hub's run/ directory",
            "summary": "Both daemons log to <hub>/run/{sync,serve}.log and the wrappers are there too. "
            "Tail those before restarting anything; the server log usually names the real failure.",
        },
        {
            "days_ago": 3,
            "tags": ["infra", "handoff"],
            "title": "Hub copy to persistent volume: dry run is green",
            "summary": "Copy plan keeps git history. Take the sync daemon down first, copy, then point "
            "the deployment config at the new path. Full steps are in the volume ticket.",
        },
        {
            "days_ago": 1,
            "tags": ["infra", "gotcha"],
            "title": "tmux sessions do not survive a host restart",
            "summary": "The console and collector are tmux sessions by convention, not a service "
            "manager. After a restart: run scripts/deploy.sh start and check status.",
        },
    ],
    "cinder": [
        {
            "days_ago": 7,
            "tags": ["quality", "api"],
            "title": "Board API: unknown column returns 400, not 422",
            "summary": "The board create/move endpoints reject unknown columns with 400 and code "
            "'invalid_column'. Tests and the console rely on that exact status.",
        },
        {
            "days_ago": 4,
            "tags": ["quality", "web"],
            "title": "Board drag animation timing is the flake",
            "summary": "The mobile screenshot test races the drag animation. Check the animation API "
            "rather than adding sleeps; see the flaky test ticket for the current hypothesis.",
        },
        {
            "days_ago": 0,
            "tags": ["quality", "demo"],
            "title": "Screenshots: synthetic hub, 1440x900 and 390x844",
            "summary": "Public screenshots must come from the generated demo hub. Desktop 1440x900 plus "
            "one mobile shot at 390x844; keep each PNG small enough for the repository.",
        },
    ],
}

BOARD_CARDS = [
    {"title": "Publish the v0.1 announcement", "desc": "Short post: what the bridge is, the git-backed hub, one screenshot. Wait for the quickstart freeze.", "column": "inbox", "assignee": None, "tags": ["docs", "release"], "author": "human", "days_ago": 5},
    {"title": "Decide whether recall should include triggers", "desc": "Recall currently returns tickets, decisions, sessions and notes. Triggers would add chat context but also noise.", "column": "inbox", "assignee": "atlas", "tags": ["product"], "author": "human", "days_ago": 3},
    {"title": "Collector: digest sessions incrementally", "desc": "Skip sessions unchanged since the cursor (path + mtime + size). Measure before/after sync time.", "column": "inbox", "assignee": "beacon", "tags": ["infra", "performance"], "author": "human", "days_ago": 2},
    {"title": "Rewrite the quickstart around the v2 console", "desc": "One path: install, generate a synthetic hub, open the console. Re-run every command offline.", "column": "in_progress", "assignee": "atlas", "tags": ["docs"], "author": "human", "days_ago": 9},
    {"title": "Harden the mobile layout pass", "desc": "Board drag animation races the screenshot test at 390px. Fix the animation API, then re-capture.", "column": "in_progress", "assignee": "cinder", "tags": ["web", "quality"], "author": "cinder", "days_ago": 5},
    {"title": "Move hub data to the persistent volume", "desc": "Stop sync, copy with git history, switch the deployment config, restart, verify health.", "column": "in_progress", "assignee": "beacon", "tags": ["infra"], "author": "beacon", "days_ago": 3},
    {"title": "Redaction corpus additions", "desc": "Add regression cases for bearer tokens in URLs and PEM blocks. Corpus must prove nothing leaks.", "column": "review", "assignee": "atlas", "tags": ["security"], "author": "human", "days_ago": 4},
    {"title": "Mobile console: 390x844 screenshot", "desc": "Capture the mobile view from the synthetic dataset; include it in the README evidence.", "column": "review", "assignee": "cinder", "tags": ["web", "demo"], "author": "human", "days_ago": 2},
    {"title": "Board API smoke tests", "desc": "Create / move / assign / delete on a temp hub, including error paths.", "column": "done", "assignee": "cinder", "tags": ["quality", "api"], "author": "cinder", "days_ago": 7},
    {"title": "Health endpoint + deploy wait", "desc": "deploy.sh waits for /api/health and tails logs on failure.", "column": "done", "assignee": "beacon", "tags": ["infra", "deploy"], "author": "beacon", "days_ago": 8},
    {"title": "SSE reconnect verified", "desc": "Console reconnects after a dropped stream; heartbeat keeps proxies alive.", "column": "done", "assignee": "cinder", "tags": ["quality"], "author": "cinder", "days_ago": 10},
]


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fake_source_id(*parts: str) -> str:
    return hashlib.sha256(("context-bridge-demo|" + "|".join(parts)).encode()).hexdigest()[:32]


def card_id(*parts: str) -> str:
    return "c_" + hashlib.sha256(("card|" + "|".join(parts)).encode()).hexdigest()[:8]


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_records(now: datetime) -> dict[str, list[Record]]:
    records: dict[str, list[Record]] = {bot: [] for bot in BOT_IDS}
    for bot in BOT_IDS:
        profile = next(b["profile"] for b in BOTS if b["id"] == bot)
        for spec in TICKETS[bot]:
            created = now - timedelta(days=spec["days_ago"], hours=2)
            updated = min(now - timedelta(hours=1), created + timedelta(hours=5))
            payload: dict = {
                "state": spec["state"],
                "mandate": {"scope": spec["title"], "source": "demo"},
                "reports": [dict(report, at=iso(created + timedelta(hours=i + 1))) for i, report in enumerate(spec["reports"])],
                "channels": ["chat"],
            }
            if spec.get("attention"):
                payload["attention"] = {"question": spec["attention"], "since": iso(updated)}
            if spec["state"] == "ended":
                payload["ended"] = iso(updated)
            record = Record(
                bot=bot,
                kind="ticket",
                title=spec["title"],
                summary=spec["summary"],
                created_at=iso(created),
                updated_at=iso(updated),
                tags=spec["tags"],
                refs={"ticket": fake_source_id(bot, "ticket", spec["slug"])},
                source={"system": "demo-superagent", "profile": profile, "source_id": fake_source_id(bot, "ticket", spec["slug"])},
                payload=payload,
            )
            records[bot].append(record)

        for spec in DECISIONS[bot]:
            created = now - timedelta(days=spec["days_ago"], hours=4)
            records[bot].append(
                Record(
                    bot=bot,
                    kind="decision",
                    title=spec["title"],
                    summary=" ".join(spec["reason"]),
                    created_at=iso(created),
                    updated_at=iso(created),
                    tags=spec["tags"],
                    source={"system": "demo-superagent", "profile": profile, "source_id": fake_source_id(bot, "decision", spec["slug"])},
                    payload={"kind": spec["kind"], "reason": spec["reason"], "strength": 2, "at": iso(created)},
                )
            )

        for spec in TRIGGERS[bot]:
            created = now - timedelta(days=spec["days_ago"], hours=7)
            records[bot].append(
                Record(
                    bot=bot,
                    kind="trigger",
                    title=spec["text"][:120],
                    summary=spec["text"],
                    created_at=iso(created),
                    updated_at=iso(created),
                    tags=["chat"],
                    source={"system": "demo-chat", "profile": profile, "source_id": fake_source_id(bot, "trigger", spec["text"])},
                    payload={"actor": spec["actor"], "human": spec["actor"] != bot, "addressed": True, "at": iso(created)},
                )
            )

        for spec in SESSIONS[bot]:
            created = now - timedelta(days=spec["days_ago"], hours=spec["hours"])
            records[bot].append(
                Record(
                    bot=bot,
                    kind="session",
                    title=spec["title"],
                    summary=spec["outcome"],
                    created_at=iso(created),
                    updated_at=iso(created + timedelta(minutes=45)),
                    tags=["session"],
                    source={"system": "demo-sessions", "profile": profile, "source_id": fake_source_id(bot, "session", spec["title"])},
                    payload={
                        "session_id": fake_source_id(bot, "session-id", spec["title"])[:16],
                        "first_request": spec["first_request"],
                        "outcome": spec["outcome"],
                        "tools": spec["tools"],
                        "duration_s": sum(spec["tools"].values()) * 40 + 300,
                        "cwd": f"work/{bot}-{spec['days_ago']}",
                    },
                )
            )

        for spec in NOTES[bot]:
            created = now - timedelta(days=spec["days_ago"], hours=6)
            records[bot].append(
                Record(
                    bot=bot,
                    kind="note",
                    title=spec["title"],
                    summary=spec["summary"],
                    created_at=iso(created),
                    updated_at=iso(created),
                    tags=spec["tags"],
                    source={"system": "ctxbridge", "profile": profile, "source_id": fake_source_id(bot, "note", spec["title"])},
                    payload={"author": bot},
                )
            )
    return records


def build_digest(bot: str, label: str, records: list[Record], day: str) -> str:
    same_day = [r for r in records if (r.created_at or "")[:10] == day]
    tickets = [r for r in same_day if r.kind == "ticket"]
    decisions = [r for r in same_day if r.kind == "decision"]
    sessions = [r for r in same_day if r.kind == "session"]
    notes = [r for r in same_day if r.kind == "note"]
    lines = [f"# {label} · {day}", ""]
    if not same_day:
        lines += ["_No recorded activity._", ""]
        return "\n".join(lines)
    lines.append(
        f"- {len(tickets)} ticket(s) · {len(decisions)} decision(s) · "
        f"{len(sessions)} session(s) · {len(notes)} note(s)"
    )
    lines.append("")
    if tickets:
        lines += ["## Tickets", ""]
        for record in tickets:
            state = (record.payload or {}).get("state", "?")
            lines.append(f"- **{record.title}** — {state}")
            if record.summary:
                lines.append(f"  {record.summary.splitlines()[0][:220]}")
        lines.append("")
    if decisions:
        lines += ["## Decisions", ""]
        for record in decisions:
            kind = (record.payload or {}).get("kind", "?")
            lines.append(f"- {kind}: **{record.title}**")
            if record.summary:
                lines.append(f"  {record.summary.splitlines()[0][:220]}")
        lines.append("")
    if sessions:
        lines += ["## Sessions", ""]
        for record in sessions:
            lines.append(f"- **{record.title}** — {record.summary.splitlines()[0][:200]}")
        lines.append("")
    if notes:
        lines += ["## Notes", ""]
        for record in notes:
            lines.append(f"- **{record.title}**")
        lines.append("")
    return "\n".join(lines)


def write_demo_config(out: Path, hub_dir: Path) -> Path:
    config_path = out / "demo.toml"
    lines = [
        "# Generated by scripts/make-demo-hub.py — synthetic demo deployment, safe to publish.",
        "# This is NOT a live deployment: bots, records and board are fictional.",
        "",
        "[hub]",
        f'dir = "{toml_escape(str(hub_dir))}"',
        "commit = true",
        "auto_push = false",
        "",
        "[server]",
        'host = "localhost"',
        "port = 8080",
        "",
        "[redaction]",
        "enabled = true",
        "mask_private_ips = true",
        "mask_internal_domains = []",
        "",
    ]
    for index, bot in enumerate(BOTS):
        state_dir = out / "states" / bot["id"]
        state_dir.mkdir(parents=True, exist_ok=True)
        lines += [
            f'[bots.{bot["id"]}]',
            f'label = "{bot["label"]}"',
            f'profile = "{bot["profile"]}"',
            f'state_dir = "{toml_escape(str(state_dir))}"',
            f'color = "{bot["color"]}"',
            f'emoji = "{bot["emoji"]}"',
            "",
        ]
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


def build_board(now: datetime) -> dict:
    cards = []
    for spec in BOARD_CARDS:
        created = now - timedelta(days=spec["days_ago"], hours=1)
        updated = min(now, created + timedelta(hours=2))
        card = BoardCard(
            id=card_id(spec["title"]),
            title=spec["title"],
            desc=spec["desc"],
            column=spec["column"],
            assignee=spec["assignee"],
            tags=spec["tags"],
            created_at=iso(created),
            updated_at=iso(updated),
            author=spec["author"],
        )
        cards.append(card.to_dict())
    column_order = {col_id: index for index, (col_id, _) in enumerate(BOARD_COLUMNS)}
    cards.sort(key=lambda card: (column_order.get(card["column"], 99), card["created_at"]))
    return {
        "columns": [{"id": col_id, "title": title} for col_id, title in BOARD_COLUMNS],
        "cards": cards,
        "updated_at": iso(now),
    }


def git_exclude(demo_dir: Path) -> None:
    """Keep a generated demo directory invisible to `git add -A` on this checkout."""
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if top.returncode != 0 or not top.stdout.strip():
        return
    repo = Path(top.stdout.strip())
    try:
        relative = demo_dir.resolve().relative_to(repo.resolve())
    except ValueError:
        return
    exclude = repo / ".git" / "info" / "exclude"
    pattern = f"/{relative.as_posix()}/"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    except OSError:
        return
    if pattern in existing:
        return
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# generated demo data (context-bridge)\n{pattern}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------
def parse_now(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make-demo-hub",
        description="Generate a synthetic, fictional Context Bridge hub for demos and screenshots.",
    )
    parser.add_argument("--out", default="./demo-hub", help="output directory (default: ./demo-hub)")
    parser.add_argument(
        "--now",
        default="",
        help="reference timestamp (ISO-8601, default: current UTC) so output is reproducible",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing output directory")
    parser.add_argument("--no-index", action="store_true", help="skip the initial search-index build")
    parser.add_argument("--no-commit", action="store_true", help="do not create the initial git commit")
    args = parser.parse_args(argv)

    now = parse_now(args.now) if args.now else datetime.now(timezone.utc)
    out = Path(args.out).expanduser().resolve()
    if out.exists():
        if not args.force:
            print(f"error: {out} already exists (use --force to replace it)", file=sys.stderr)
            return 1
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    hub_dir = out / "hub"

    # A hub created by Hub(None, ...) is a real hub layout; records/digests are then filled
    # with synthetic content, and the manifest is rewritten with the fictional fleet.
    hub = Hub(None, root=hub_dir)
    hub.ensure()
    records_by_bot = build_records(now)
    all_records = [record for bot in BOT_IDS for record in records_by_bot[bot]]
    hub.write_records(all_records)

    for bot in BOT_IDS:
        label = next(b["label"] for b in BOTS if b["id"] == bot)
        for day_offset in range(0, 3):
            day = (now - timedelta(days=day_offset)).strftime("%Y-%m-%d")
            hub.write_digest(bot, day, build_digest(bot, label, records_by_bot[bot], day))

    board = build_board(now)
    hub.write_json(hub.board_file, board)

    hub.write_manifest(
        {
            "bots": {bot["id"]: dict(bot) for bot in BOTS},
            "demo": True,
            "last_sync": {"at": iso(now - timedelta(minutes=2)), "records": len(all_records)},
        }
    )
    hub.write_json(
        hub.state_dir / "last-sync.json",
        {"at": iso(now - timedelta(minutes=2)), "records": len(all_records), "demo": True},
    )
    hub.write_redaction_stats(
        {
            "total": 31,
            "by_rule": {
                "api_key": 4,
                "bearer_token": 2,
                "private_ip": 9,
                "internal_domain": 16,
            },
            "updated_at": iso(now),
        }
    )
    hub.write_cursors({})

    if not args.no_commit:
        try:
            hub.commit("demo: synthetic dataset for screenshots", author="ctxbridge-demo")
        except Exception as exc:  # git is optional for a read-only demo
            print(f"warning: initial git commit skipped ({exc})", file=sys.stderr)

    config_path = write_demo_config(out, hub_dir)
    git_exclude(out)

    if not args.no_index:
        command = [sys.executable, "-m", "context_bridge.cli", "--config", str(config_path), "index"]
        try:
            result = subprocess.run(
                command, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                print("note: search index will be built by the server on first search", file=sys.stderr)
        except (OSError, subprocess.TimeoutExpired):
            print("note: search index will be built by the server on first search", file=sys.stderr)

    counts = {}
    for record in all_records:
        counts[record.kind] = counts.get(record.kind, 0) + 1
    summary = ", ".join(f"{kind} {count}" for kind, count in sorted(counts.items()))
    print(f"demo hub:  {hub_dir}")
    print(f"records:   {len(all_records)} ({summary})")
    print(f"board:     {len(board['cards'])} cards, {len(BOT_IDS)} bots, 3 days of digests")
    print(f"config:    {config_path}")
    print("")
    print("serve it with:")
    print(f"  python3 -m context_bridge.cli --config {config_path} serve")
    print("  # then open http://localhost:8080/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
