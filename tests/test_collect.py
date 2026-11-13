"""Collector tests: superagent sqlite fixtures and zstd DSH session fixtures.

Everything runs in ``tmp_path`` — no real bot state, no network. The session tests skip when no
``zstd`` decoder is available (the deployment host provides one via ``$CTXBRIDGE_ZSTD``).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from context_bridge.collect import sessions as session_collector
from context_bridge.collect import superagent as superagent_collector
from context_bridge.collect.superagent import SqliteTooOldError, sqlite_version_ok
from context_bridge.config import parse_config
from context_bridge.redact import MASK, Redactor

from conftest import make_superagent_db

HEX32 = "0123456789abcdef" * 2  # placeholders / ids only: 32 hex chars
HEX32B = "fedcba9876543210" * 2


# -- builders (no literal credential shapes in source) ---------------------------------


def openai_key() -> str:
    return "s" + "k-" + "proj-" + "Zx9" * 11


def password_assignment(value: str = "hunter2xyz") -> str:
    return "password = " + value


def private_ip() -> str:
    return "10." + "55.0.7"


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_config(tmp_path: Path, *, sessions_root: str | None = None):
    state_a = tmp_path / "state-a"
    state_b = tmp_path / "state-b"
    state_a.mkdir(exist_ok=True)
    state_b.mkdir(exist_ok=True)
    bots = {
        "alpha": {"label": "Alpha", "profile": "superagent", "state_dir": str(state_a)},
        "beta": {"label": "Beta", "profile": "superagent-beta", "state_dir": str(state_b)},
    }
    if sessions_root is not None:
        for bot in bots.values():
            bot["sessions_root"] = sessions_root
    return parse_config(
        {
            "hub": {"dir": str(tmp_path / "hub"), "commit": False},
            "bots": bots,
        }
    )


def ticket_value(
    ticket_id: str,
    title: str = "a ticket",
    *,
    updated_at: str = "2026-11-01T10:00:00.000Z",
    created_at: str = "2026-11-01T09:00:00.000Z",
    assignments: list[dict] | None = None,
    ended: dict | None = None,
    attention: dict | None = None,
) -> dict:
    value = {
        "id": ticket_id,
        "title": title,
        "decisionId": "d" * 32,
        "mandate": {
            "strength": "explicit",
            "scope": title,
            "basis": {
                "source": "feishu",
                "row": "feishu:oc_EXAMPLE:om_EXAMPLE",
                "sender": "Tester",
                "via": "addressed",
                "addressed": "mention",
            },
            "actor": {"source": "feishu", "id": "ou_EXAMPLE"},
        },
        "channels": [{"source": "feishu", "thread": "feishu:oc_EXAMPLE:om_EXAMPLE"}],
        "assignments": assignments or [],
        "inputs": [],
        "undelivered": [],
        "origin": {"triggerId": "t" * 32},
        "execution": {"name": "work-" + ticket_id[:8]},
        "createdAt": created_at,
        "updatedAt": updated_at,
    }
    if ended is not None:
        value["ended"] = ended
    if attention is not None:
        value["attention"] = attention
    return value


def trigger_value(text: str, at: str = "2026-11-01T10:00:00.000Z", human: bool = True) -> dict:
    return {
        "id": "trig-" + hashlib.sha256(text.encode()).hexdigest()[:12],
        "source": "feishu",
        "threads": [{"source": "feishu", "id": "oc_EXAMPLE:om_EXAMPLE"}],
        "actor": {"source": "feishu", "id": "ou_EXAMPLE"},
        "human": human,
        "addressed": "mention" if human else None,
        "at": at,
        "text": text,
        "channel": {"source": "feishu", "thread": "feishu:oc_EXAMPLE:om_EXAMPLE"},
        "meta": {"chatId": "oc_EXAMPLE", "messageId": "om_EXAMPLE", "direct": False},
    }


def decision_value(
    kind: str = "accept", at: str = "2026-11-01T10:00:00.000Z", reason_text: str = "explicit request"
) -> dict:
    return {
        "id": "dec-" + hashlib.sha256(kind.encode()).hexdigest()[:12],
        "evalId": "e" * 32,
        "threadKey": "feishu:oc_EXAMPLE:om_EXAMPLE",
        "triggerIds": ["t" * 32],
        "policyVersion": "p" * 32,
        "at": at,
        "kind": kind,
        "reason": [{"code": "said", "values": {"text": reason_text}}],
        "strength": "explicit",
    }


def _write_rows(state_dir: Path, rows: dict[str, list[dict]]) -> Path:
    """Wrap raw values the way ``make_superagent_db`` expects (``{"id": ..., "value": ...}``)."""
    wrapped = {
        table: [{"id": item["id"], "value": item} for item in items] for table, items in rows.items()
    }
    return make_superagent_db(state_dir / "orchestrator.sqlite", wrapped)


# -- superagent collector ----------------------------------------------------------------


def test_collect_maps_ticket_trigger_decision(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state_a = Path(config.bots["alpha"].state_dir)
    running = {
        "id": "a" * 32,
        "state": "running",
        "createdAt": "2026-11-01T09:05:00.000Z",
        "report": {"state": "progress", "summary": "halfway through", "at": "2026-11-01T10:30:00.000Z"},
    }
    completed = {
        "id": "b" * 32,
        "state": "ended",
        "report": {"state": "ready", "summary": "done work", "at": "2026-11-01T11:00:00.000Z"},
        "endedAt": "2026-11-01T11:00:00.000Z",
    }
    tickets = [
        ticket_value(
            "a" * 32,
            "open ticket",
            updated_at="2026-11-01T10:30:00.000Z",
            assignments=[running],
            attention={"id": "n" * 32, "question": "which environment?", "triggerId": "t" * 32},
        ),
        ticket_value(
            "b" * 32,
            "ended ticket",
            updated_at="2026-11-01T11:00:00.000Z",
            assignments=[completed],
            ended={"at": "2026-11-01T11:00:00.000Z", "by": "executor", "outcome": "completed"},
        ),
    ]
    triggers = [
        trigger_value("please look at the bridge", at="2026-11-01T10:01:00.000Z", human=True),
        trigger_value("agent ping", at="2026-11-01T10:02:00.000Z", human=False),
    ]
    decisions = [decision_value("accept", at="2026-11-01T10:03:00.000Z")]
    _write_rows(state_a, {"ticket": tickets, "trigger": triggers, "decision": decisions})

    records, cursors = superagent_collector.collect(config, {}, Redactor())

    by_kind: dict[str, list] = {}
    for record in records:
        by_kind.setdefault(record.kind, []).append(record)
    assert len(by_kind.get("ticket", [])) == 2
    assert len(by_kind.get("trigger", [])) == 2
    assert len(by_kind.get("decision", [])) == 1
    assert {record.bot for record in records} == {"alpha"}
    for record in records:
        assert record.id.startswith(f"alpha:{record.kind}:")
        assert record.source["system"] == "dsh-superagent"
        assert record.source["source_id"]

    open_ticket = next(r for r in by_kind["ticket"] if r.title == "open ticket")
    assert open_ticket.payload["state"] == "open"
    assert open_ticket.payload["active"] is True
    assert open_ticket.payload["latest_report"]["state"] == "progress"
    assert open_ticket.payload["reports"][-1]["summary"] == "halfway through"
    assert "halfway through" in open_ticket.summary
    assert open_ticket.payload["attention"]["question"] == "which environment?"
    assert "attention" in open_ticket.tags
    assert open_ticket.refs["ticket"] == "a" * 32

    ended_ticket = next(r for r in by_kind["ticket"] if r.title == "ended ticket")
    assert ended_ticket.payload["state"] == "ended"
    assert ended_ticket.payload["ended"]["outcome"] == "completed"
    assert "done work" in ended_ticket.summary

    human_trigger = next(r for r in by_kind["trigger"] if r.payload["human"] is True)
    assert human_trigger.payload["addressed"] == "mention"
    assert human_trigger.title == "please look at the bridge"
    agent_trigger = next(r for r in by_kind["trigger"] if r.payload["human"] is False)
    assert agent_trigger.payload["human"] is False

    decision = by_kind["decision"][0]
    assert "accept" in decision.title
    assert decision.payload["kind"] == "accept"
    assert decision.payload["strength"] == "explicit"

    assert cursors["superagent"]["alpha"]["ticket"]["watermark"] == "2026-11-01T11:00:00.000Z"
    assert cursors["superagent"]["alpha"]["trigger"]["watermark"] == "2026-11-01T10:02:00.000Z"
    assert cursors["superagent"]["alpha"]["decision"]["watermark"] == "2026-11-01T10:03:00.000Z"


def test_collect_is_idempotent_and_incremental(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    db = Path(config.bots["alpha"].state_dir) / "orchestrator.sqlite"
    _write_rows(
        Path(config.bots["alpha"].state_dir),
        {
            "ticket": [ticket_value("a" * 32, "first", updated_at="2026-11-01T10:00:00.000Z")],
            "trigger": [trigger_value("hello", at="2026-11-01T10:00:00.000Z")],
            "decision": [decision_value("accept", at="2026-11-01T10:00:00.000Z")],
        },
    )
    first_records, cursors = superagent_collector.collect(config, {}, Redactor())
    assert len(first_records) == 3

    second_records, cursors = superagent_collector.collect(config, cursors, Redactor())
    assert second_records == []

    # Change one ticket and append one trigger; only those two are re-exported.
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE ticket SET value = ? WHERE id = ?",
        (
            json.dumps(ticket_value("a" * 32, "first updated", updated_at="2026-11-02T12:00:00.000Z")),
            "a" * 32,
        ),
    )
    con.execute(
        "INSERT INTO trigger (id, value, at) VALUES (?, ?, ?)",
        (
            "new-trigger",
            json.dumps(trigger_value("new message", at="2026-11-02T12:30:00.000Z")),
            "2026-11-02T12:30:00.000Z",
        ),
    )
    con.commit()
    con.close()

    third_records, cursors = superagent_collector.collect(config, cursors, Redactor())
    kinds = sorted(record.kind for record in third_records)
    assert kinds == ["ticket", "trigger"]
    assert next(r for r in third_records if r.kind == "ticket").title == "first updated"
    assert cursors["superagent"]["alpha"]["ticket"]["watermark"] == "2026-11-02T12:00:00.000Z"
    assert cursors["superagent"]["alpha"]["trigger"]["watermark"] == "2026-11-02T12:30:00.000Z"


def test_collected_text_is_redacted(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    secret = openai_key()
    _write_rows(
        Path(config.bots["alpha"].state_dir),
        {
            "ticket": [
                ticket_value(
                    "a" * 32,
                    f"deploy with {secret}",
                    updated_at="2026-11-01T10:00:00.000Z",
                )
            ],
            "trigger": [trigger_value(f"db {password_assignment()} please", at="2026-11-01T10:00:00.000Z")],
            "decision": [
                decision_value(
                    "accept", at="2026-11-01T10:00:00.000Z", reason_text=f"reached {private_ip()}"
                )
            ],
        },
    )
    redactor = Redactor()
    records, _ = superagent_collector.collect(config, {}, redactor)
    dumped = json.dumps([record.to_dict() for record in records], ensure_ascii=False)
    for fragment in (secret, "hunter2xyz", private_ip()):
        assert fragment not in dumped, f"secret {fragment!r} survived in record payload"
    assert MASK in dumped
    assert sum(record.redactions for record in records) >= 3
    by_rule = redactor.stats()["by_rule"]
    assert by_rule.get("openai_key", 0) >= 1
    assert by_rule.get("credential_assignment", 0) >= 1
    assert by_rule.get("private_ip", 0) >= 1


def test_missing_state_dir_is_skipped(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    records, cursors = superagent_collector.collect(config, {}, Redactor())
    assert records == []
    assert "superagent" in cursors


def test_sqlite_version_probe_and_actionable_error(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    _write_rows(Path(config.bots["alpha"].state_dir), {"ticket": []})
    assert isinstance(sqlite_version_ok(), bool)
    assert sqlite_version_ok() == (tuple(sqlite3.sqlite_version_info) >= (3, 38))

    monkeypatch.setattr(superagent_collector, "sqlite_version_ok", lambda: False)
    monkeypatch.setattr(superagent_collector, "_schema_needs_newer_sqlite", lambda con: True)
    with pytest.raises(SqliteTooOldError, match="SQLite >= 3.38"):
        superagent_collector.collect(config, {}, Redactor())


# -- sessions collector ------------------------------------------------------------------


def _zstd() -> str:
    try:
        return session_collector.zstd_binary()
    except session_collector.ZstdMissingError:  # pragma: no cover - host without zstd
        pytest.skip("zstd decoder not available")


def compress_jsonl(binary: str, payload: Path, lines: list[dict | str]) -> Path:
    payload.parent.mkdir(parents=True, exist_ok=True)
    raw = payload.parent / (payload.name + ".tmp.jsonl")
    raw.write_text(
        "\n".join(line if isinstance(line, str) else json.dumps(line, ensure_ascii=False) for line in lines)
        + "\n",
        encoding="utf-8",
    )
    out = payload
    proc = subprocess.run([binary, "-q", "-f", str(raw), "-o", str(out)], capture_output=True)
    assert proc.returncode == 0, proc.stderr[:300]
    return out


def session_lines(
    cwd: str,
    *,
    session_id: str = "session-1",
    title: str = "Fix the bridge",
    request: str = "please fix the bridge",
    outcome: str = "Bridge fixed; PR opened.",
    created_ms: int | None = None,
    tools: tuple[str, ...] = ("bash", "bash", "read"),
) -> list[dict | str]:
    relay = json.dumps(
        {"ticket_id": "t" * 32, "title": title, "trigger": {"text": request, "from": "ou_EXAMPLE"}},
        ensure_ascii=False,
    )
    lines: list[dict | str] = [
        {
            "type": "session",
            "version": 4,
            "id": session_id,
            "createdAt": created_ms if created_ms is not None else now_ms(),
            "cwd": cwd,
            "agentPreset": "dsh-superagent-worker",
            "delegationDepth": 0,
        },
        {"type": "session/title", "seq": 1, "data": {"title": title}},
        {
            "type": "user/message",
            "seq": 2,
            "data": {"role": "user", "content": [{"type": "text", "text": relay}]},
        },
        {
            "type": "request/header",
            "seq": 3,
            "data": {"header": {"config": {"provider": "test-provider", "model": "test-model"}}},
        },
        {"type": "turn/start", "seq": 4, "data": {"turn": 1}},
    ]
    for index, name in enumerate(tools):
        lines.append({"type": "tool/call", "seq": 10 + index, "data": {"name": name, "arguments": "{}"}})
    lines.append(
        {
            "type": "assistant/message",
            "seq": 20,
            "data": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "text": "thinking out loud"},
                        {"type": "text", "text": outcome},
                    ],
                }
            },
        }
    )
    lines.append({"type": "turn/end", "seq": 21, "data": {"turn": 1, "reason": {"kind": "completed"}}})
    return lines


def test_sessions_digest_and_attribution(tmp_path: Path) -> None:
    binary = _zstd()
    sessions_root = tmp_path / "sessions"
    config = make_config(tmp_path, sessions_root=str(sessions_root))
    state_a = Path(config.bots["alpha"].state_dir)
    state_b = Path(config.bots["beta"].state_dir)
    compress_jsonl(
        binary,
        sessions_root / "--state-a--" / "s1" / "session.v4.jsonl.zstd",
        session_lines(str(state_a) + "/work/ticket-1", session_id="s1"),
    )
    compress_jsonl(
        binary,
        sessions_root / "--state-b--" / "s2" / "session.v4.jsonl.zstd",
        session_lines(str(state_b) + "/work/ticket-2", session_id="s2", title="Beta work", outcome="Done."),
    )
    # A session whose cwd only *string*-prefixes alpha's state dir must not be attributed.
    compress_jsonl(
        binary,
        sessions_root / "--sibling--" / "s3" / "session.v4.jsonl.zstd",
        session_lines(str(state_a) + "-sibling/work/x", session_id="s3"),
    )

    records, cursors = session_collector.collect(config, {}, Redactor())
    assert len(records) == 2
    by_bot = {record.bot: record for record in records}
    assert set(by_bot) == {"alpha", "beta"}
    alpha = by_bot["alpha"]
    assert alpha.kind == "session"
    assert alpha.title == "Fix the bridge"
    assert "please fix the bridge" in alpha.summary
    assert "Bridge fixed; PR opened." in alpha.summary
    assert alpha.payload["tool_calls"] == 3
    assert alpha.payload["tools"] == {"bash": 2, "read": 1}
    assert alpha.payload["turns"] == 1
    assert alpha.payload["model"]["model"] == "test-model"
    assert alpha.payload["ended_reason"] == "completed"
    assert by_bot["beta"].title == "Beta work"
    assert "sessions" in cursors

    second_records, _ = session_collector.collect(config, cursors, Redactor())
    assert second_records == []


def test_sessions_incremental_on_change(tmp_path: Path) -> None:
    binary = _zstd()
    sessions_root = tmp_path / "sessions"
    config = make_config(tmp_path, sessions_root=str(sessions_root))
    state_a = Path(config.bots["alpha"].state_dir)
    path = sessions_root / "--state-a--" / "s1" / "session.v4.jsonl.zstd"
    lines = session_lines(str(state_a) + "/work/t", session_id="s1")
    compress_jsonl(binary, path, lines)
    records, cursors = session_collector.collect(config, {}, Redactor())
    assert len(records) == 1

    # Grow the transcript (new assistant text) and rewrite it: it must be re-digested.
    grown = lines + [
        {
            "type": "assistant/message",
            "seq": 30,
            "data": {"message": {"role": "assistant", "content": [{"type": "text", "text": "Follow-up: merged."}]}},
        }
    ]
    compress_jsonl(binary, path, grown)
    records, _ = session_collector.collect(config, cursors, Redactor())
    assert len(records) == 1
    assert "Follow-up: merged." in records[0].summary


def test_sessions_window(tmp_path: Path) -> None:
    binary = _zstd()
    sessions_root = tmp_path / "sessions"
    config = make_config(tmp_path, sessions_root=str(sessions_root))
    state_a = Path(config.bots["alpha"].state_dir)
    old_moment = datetime.now(timezone.utc) - timedelta(days=200)
    path = sessions_root / "--state-a--" / "old" / "session.v4.jsonl.zstd"
    compress_jsonl(binary, path, session_lines(str(state_a), session_id="old", created_ms=int(old_moment.timestamp() * 1000)))
    stamp = old_moment.timestamp()
    os.utime(path, (stamp, stamp))

    records, cursors = session_collector.collect(config, {}, Redactor())
    assert records == []
    records, _ = session_collector.collect(config, cursors, Redactor(), window_days=365)
    assert len(records) == 1
    assert records[0].payload["session_id"] == "old"


def test_sessions_tolerates_corrupt_and_headerless_streams(tmp_path: Path) -> None:
    binary = _zstd()
    sessions_root = tmp_path / "sessions"
    config = make_config(tmp_path, sessions_root=str(sessions_root))
    state_a = Path(config.bots["alpha"].state_dir)

    corrupt = sessions_root / "--state-a--" / "bad" / "session.v4.jsonl.zstd"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"this is not a zstd stream")

    headerless = sessions_root / "--state-a--" / "empty" / "session.v4.jsonl.zstd"
    compress_jsonl(binary, headerless, [{"type": "turn/start", "seq": 0, "data": {}}])

    torn = sessions_root / "--state-a--" / "torn" / "session.v4.jsonl.zstd"
    torn_lines: list[dict | str] = list(session_lines(str(state_a), session_id="torn"))
    torn_lines.insert(3, "{not valid json")
    compress_jsonl(binary, torn, torn_lines)

    records, _ = session_collector.collect(config, {}, Redactor())
    assert [record.payload["session_id"] for record in records] == ["torn"]


def test_zstd_binary_resolution(tmp_path: Path, monkeypatch) -> None:
    binary = _zstd()
    monkeypatch.setenv(session_collector.ZSTD_ENV, binary)
    assert session_collector.zstd_binary() == binary

    monkeypatch.setenv(session_collector.ZSTD_ENV, str(tmp_path / "missing-zstd"))
    with pytest.raises(session_collector.ZstdMissingError):
        session_collector.zstd_binary()

    monkeypatch.delenv(session_collector.ZSTD_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    with pytest.raises(session_collector.ZstdMissingError, match="set .*CTXBRIDGE_ZSTD"):
        session_collector.zstd_binary()


def test_sessions_attribute_through_symlinked_state_dir(tmp_path: Path) -> None:
    binary = _zstd()
    sessions_root = tmp_path / "sessions"
    config = make_config(tmp_path, sessions_root=str(sessions_root))
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    link = tmp_path / "linked-state"
    try:
        link.symlink_to(real_state, target_is_directory=True)
    except OSError:  # pragma: no cover - filesystem without symlink support
        pytest.skip("symlinks unavailable")
    config.bots["alpha"].state_dir = str(link)
    config.bots["beta"].state_dir = str(tmp_path / "state-b")
    compress_jsonl(
        binary,
        sessions_root / "--real-session--" / "s1" / "session.v4.jsonl.zstd",
        session_lines(str(real_state) + "/work/ticket-1", session_id="s1"),
    )
    records, _ = session_collector.collect(config, {}, Redactor())
    assert [record.bot for record in records] == ["alpha"]


def test_sessions_missing_root_is_harmless(tmp_path: Path) -> None:
    config = make_config(tmp_path, sessions_root=str(tmp_path / "does-not-exist"))
    _zstd()  # fail early if the host has no decoder
    records, _ = session_collector.collect(config, {}, Redactor())
    assert records == []
