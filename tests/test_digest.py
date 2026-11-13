"""Digest tests plus one end-to-end ``run_sync`` exercise against a temp hub.

No network, no real bot state: the sqlite fixture is built with ``tests/conftest.py`` helpers and
everything is written under ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from context_bridge.config import ConfigError
from context_bridge.digest import build_and_write_digests, build_digest
from context_bridge.hub import Hub
from context_bridge.model import Record
from context_bridge.sync import redaction_stats_totals, run_sync

from conftest import make_superagent_db

DAY = "2026-11-13"


def rec(
    bot: str = "alpha",
    kind: str = "ticket",
    title: str = "a record",
    summary: str = "",
    *,
    created: str = f"{DAY}T09:00:00.000Z",
    updated: str | None = None,
    payload: dict | None = None,
    tags: list[str] | None = None,
) -> Record:
    return Record(
        bot=bot,
        kind=kind,
        title=title,
        summary=summary,
        created_at=created,
        updated_at=updated or created,
        payload=payload or {},
        tags=tags or [],
        source={"source_id": title},
    )


class FakeHub:
    """Minimal hub stand-in for digest unit tests (no git, no disk)."""

    def __init__(self, records_by_bot: dict[str, list[Record]], bots: dict | None = None) -> None:
        self._records = records_by_bot
        self.config = SimpleNamespace(bots=bots or {})
        self.written: dict[tuple[str, str], str] = {}

    def iter_records(self, bot: str | None = None, kind: str | None = None):
        for record in self._records.get(bot, []):
            if kind is None or record.kind == kind:
                yield record

    def write_digest(self, bot: str, date: str, markdown: str) -> Path:
        self.written[(bot, date)] = markdown
        return Path("digests") / bot / f"{date}.md"


# -- build_digest ------------------------------------------------------------------------


def test_digest_sections_and_determinism() -> None:
    records = [
        rec(kind="ticket", title="ship bridge", updated=f"{DAY}T10:00:00.000Z", payload={"state": "open"}),
        rec(
            kind="ticket",
            title="fix bridge",
            updated=f"{DAY}T11:00:00.000Z",
            payload={"state": "ended", "attention": {"question": "which env?"}},
        ),
        rec(kind="decision", title="accept: do it", summary="explicit request", payload={"kind": "accept"}),
        rec(kind="session", title="session one", summary="**Outcome:** done"),
        rec(kind="note", title="handoff", summary="hub is ready"),
    ]
    markdown = build_digest("alpha", records, DAY)
    assert markdown.startswith(f"# alpha — daily digest {DAY}")
    for expected in ("## Tickets (2)", "## Decisions (1)", "## Sessions (1)", "## Notes (1)"):
        assert expected in markdown
    assert "**ship bridge** · open" in markdown
    assert "which env?" in markdown
    assert "**session one**" in markdown
    assert "**handoff**: hub is ready" in markdown
    # Deterministic: same input, same bytes, regardless of input order.
    assert build_digest("alpha", records, DAY) == markdown
    assert build_digest("alpha", list(reversed(records)), DAY) == markdown


def test_digest_filters_bot_and_date() -> None:
    records = [
        rec(kind="ticket", title="today", created=f"{DAY}T08:00:00.000Z"),
        rec(kind="ticket", title="yesterday", created="2026-11-12T08:00:00.000Z"),
        rec(kind="ticket", title="beta work", bot="beta", created=f"{DAY}T08:00:00.000Z"),
    ]
    markdown = build_digest("alpha", records, DAY)
    assert "today" in markdown
    assert "beta work" not in markdown
    assert "yesterday" not in markdown
    assert "1 hub record" in markdown


def test_digest_empty_day() -> None:
    markdown = build_digest("alpha", [], DAY)
    assert f"# alpha — daily digest {DAY}" in markdown
    assert f"No hub records dated {DAY}" in markdown
    assert "## Tickets" not in markdown


def test_digest_caps_long_sections() -> None:
    tickets = [rec(kind="ticket", title=f"ticket {i:02d}") for i in range(35)]
    sessions = [rec(kind="session", title=f"session {i:02d}") for i in range(20)]
    markdown = build_digest("alpha", tickets + sessions, DAY)
    assert "## Tickets (35)" in markdown
    assert "… and 5 more" in markdown
    assert "## Sessions (20)" in markdown
    assert "… and 5 more" in markdown


# -- build_and_write_digests -------------------------------------------------------------


def test_build_and_write_digests_uses_hub() -> None:
    records = {"alpha": [rec(bot="alpha", kind="note", title="alpha note")], "beta": []}
    hub = FakeHub(records, bots={"alpha": SimpleNamespace(), "beta": SimpleNamespace()})
    written = build_and_write_digests(hub, {"alpha": None, "beta": None}, date=DAY)
    assert set(written) == {"alpha", "beta"}
    assert written["alpha"] == Path("digests") / "alpha" / f"{DAY}.md"
    assert "alpha note" in hub.written[("alpha", DAY)]
    assert "No hub records" in hub.written[("beta", DAY)]
    # Date defaults to today (UTC) when omitted.
    written_default = build_and_write_digests(hub, {"alpha": None})
    today = datetime.now(timezone.utc).date().isoformat()
    assert written_default["alpha"] == Path("digests") / "alpha" / f"{today}.md"


# -- run_sync end to end -----------------------------------------------------------------


def _write_alpha_db(config) -> None:
    state_a = Path(config.bots["alpha"].state_dir)
    ticket = {
        "id": "a" * 32,
        "title": "bridge ticket",
        "decisionId": "d" * 32,
        "mandate": {"strength": "explicit", "scope": "bridge ticket"},
        "channels": [],
        "assignments": [
            {
                "id": "s" * 32,
                "state": "ended",
                "report": {"state": "ready", "summary": "bridge shipped", "at": f"{DAY}T10:00:00.000Z"},
            }
        ],
        "inputs": [],
        "undelivered": [],
        "origin": {"triggerId": "t" * 32},
        "execution": {"name": "work-a"},
        "createdAt": f"{DAY}T08:00:00.000Z",
        "updatedAt": f"{DAY}T10:00:00.000Z",
        "ended": {"at": f"{DAY}T10:00:00.000Z", "by": "executor", "outcome": "completed"},
    }
    trigger = {
        "id": "b" * 32,
        "source": "feishu",
        "threads": [],
        "actor": {"source": "feishu", "id": "ou_EXAMPLE"},
        "human": True,
        "at": f"{DAY}T08:00:00.000Z",
        "text": "please build the bridge",
        "channel": {"source": "feishu"},
        "meta": {},
    }
    decision = {
        "id": "c" * 32,
        "threadKey": "feishu:oc_EXAMPLE:om_EXAMPLE",
        "triggerIds": ["b" * 32],
        "at": f"{DAY}T08:01:00.000Z",
        "kind": "accept",
        "reason": [{"code": "said", "values": {"text": "explicit request"}}],
        "strength": "explicit",
    }
    make_superagent_db(
        state_a / "orchestrator.sqlite",
        {
            "ticket": [{"id": ticket["id"], "value": ticket}],
            "trigger": [{"id": trigger["id"], "value": trigger}],
            "decision": [{"id": decision["id"], "value": decision}],
        },
    )


def test_run_sync_end_to_end(config) -> None:
    _write_alpha_db(config)
    summary = run_sync(config, with_sessions=False)
    assert set(summary) == {"records", "per_bot", "committed", "at"}
    assert summary["records"] == 3
    assert summary["per_bot"] == {"alpha": 3, "beta": 0}
    assert summary["committed"] is False  # fixture config has hub.commit = false

    hub = Hub(config)
    alpha_records = list(hub.iter_records(bot="alpha"))
    assert sorted(record.kind for record in alpha_records) == ["decision", "ticket", "trigger"]
    assert list(hub.iter_records(bot="beta")) == []

    cursors = hub.read_cursors()
    assert cursors["superagent"]["alpha"]["ticket"]["watermark"] == f"{DAY}T10:00:00.000Z"
    manifest = hub.read_manifest()
    assert manifest["last_sync"]["at"] == summary["at"]
    assert manifest["last_sync"]["records"] == 3
    assert set(manifest["bots"]) == {"alpha", "beta"}
    assert manifest["v"] == 1

    redaction = hub.read_redaction_stats()
    assert redaction["total"] == 0
    assert redaction["by_rule"] == {}
    assert redaction["first_synced_at"] and redaction["last_synced_at"]

    today = datetime.now(timezone.utc).date().isoformat()
    digest = hub.read_digest("alpha", today)
    assert digest is not None
    assert digest.startswith(f"# alpha — daily digest {today}")
    assert "bridge ticket" in digest

    # Second pass is incremental: nothing new, nothing committed.
    again = run_sync(config, with_sessions=False)
    assert again["records"] == 0
    assert again["per_bot"] == {"alpha": 0, "beta": 0}
    assert again["committed"] is False


def test_run_sync_rejects_unknown_bot(config) -> None:
    with pytest.raises(ConfigError, match="unknown bot"):
        run_sync(config, bots=["nope"], with_sessions=False)


def test_run_sync_filtered_bot(config) -> None:
    _write_alpha_db(config)
    full = run_sync(config, with_sessions=False)
    assert full["records"] == 3

    summary = run_sync(config, bots=["beta"], with_sessions=False)
    assert summary["per_bot"] == {"beta": 0}
    assert summary["records"] == 0
    # Alpha's records are untouched; its cursor survives the filtered run.
    hub = Hub(config)
    assert len(list(hub.iter_records(bot="alpha"))) == 3
    assert "alpha" in hub.read_cursors()["superagent"]
    # The manifest registry always covers every configured bot, not just the selected one.
    assert set(hub.read_manifest()["bots"]) == {"alpha", "beta"}


# -- cumulative redaction stats -----------------------------------------------------------


def openai_key() -> str:
    return "s" + "k-" + "proj-" + "Zx9" * 11


def aws_key() -> str:
    return "AK" + "IA" + "A1B2C3D4E5F6G7H8"


def _write_ticket_title(config, title: str, updated_at: str) -> None:
    """(Re)create alpha's state DB with a single ticket whose title is ``title``."""
    state_a = Path(config.bots["alpha"].state_dir)
    db = state_a / "orchestrator.sqlite"
    if db.exists():
        db.unlink()
    ticket = {
        "id": "a" * 32,
        "title": title,
        "decisionId": "d" * 32,
        "mandate": {"strength": "explicit", "scope": title},
        "channels": [],
        "assignments": [],
        "inputs": [],
        "undelivered": [],
        "origin": {"triggerId": "t" * 32},
        "execution": {"name": "work-a"},
        "createdAt": f"{DAY}T08:00:00.000Z",
        "updatedAt": updated_at,
    }
    make_superagent_db(db, {"ticket": [{"id": ticket["id"], "value": ticket}]})


def test_redaction_stats_accumulate_across_incremental_syncs(config) -> None:
    hub = Hub(config)
    _write_ticket_title(config, f"deploy with {openai_key()}", f"{DAY}T10:00:00.000Z")
    first = run_sync(config, with_sessions=False)
    assert first["records"] == 1

    stats_one = hub.read_redaction_stats()
    assert stats_one["total"] >= 1
    assert stats_one["total"] == sum(stats_one["by_rule"].values())
    assert "openai_key" in stats_one["by_rule"]
    assert stats_one["first_synced_at"] == stats_one["last_synced_at"]

    # An incremental pass that masks nothing must NOT reset the running total (the dashboard bug).
    second = run_sync(config, with_sessions=False)
    assert second["records"] == 0
    stats_two = hub.read_redaction_stats()
    assert stats_two["total"] == stats_one["total"]
    assert stats_two["by_rule"] == stats_one["by_rule"]
    assert stats_two["first_synced_at"] == stats_one["first_synced_at"]
    assert stats_two["last_synced_at"] >= stats_one["last_synced_at"]

    # A later change adds to the total and never lowers a per-rule counter.
    _write_ticket_title(config, f"deploy with {aws_key()}", f"{DAY}T12:00:00.000Z")
    third = run_sync(config, with_sessions=False)
    assert third["records"] == 1
    stats_three = hub.read_redaction_stats()
    assert stats_three["total"] > stats_two["total"]
    assert stats_three["total"] == sum(stats_three["by_rule"].values())
    for rule, count in stats_two["by_rule"].items():
        assert stats_three["by_rule"][rule] >= count
    assert "aws_access_key" in stats_three["by_rule"]
    assert stats_three["first_synced_at"] == stats_one["first_synced_at"]

    # Shape stays compatible with what /api/health and `ctxbridge status` expect.
    assert isinstance(stats_three["total"], int)
    assert isinstance(stats_three["by_rule"], dict)
    status = Hub(config).status()
    assert isinstance(status, dict) and "records" in status


def test_redaction_stats_totals_helper() -> None:
    first = redaction_stats_totals({}, {"total": 2, "by_rule": {"internal_domain": 2}}, "2026-11-13T10:00:00.000Z")
    assert first == {
        "total": 2,
        "by_rule": {"internal_domain": 2},
        "first_synced_at": "2026-11-13T10:00:00.000Z",
        "last_synced_at": "2026-11-13T10:00:00.000Z",
    }
    second = redaction_stats_totals(first, {"total": 0, "by_rule": {}}, "2026-11-13T10:01:00.000Z")
    assert second["total"] == 2
    assert second["by_rule"] == {"internal_domain": 2}
    assert second["first_synced_at"] == first["first_synced_at"]
    third = redaction_stats_totals(
        second, {"total": 3, "by_rule": {"home_path": 3}}, "2026-11-13T10:02:00.000Z"
    )
    assert third["total"] == 5
    assert third["by_rule"] == {"home_path": 3, "internal_domain": 2}
    # Defensive: a corrupt/legacy counter file must not crash a sync.
    assert redaction_stats_totals({"total": "junk", "by_rule": "junk"}, None, "t")["total"] == 0
