"""Collector for ``dsh-superagent`` bots.

Each bot's state directory holds ``orchestrator.sqlite`` with JSON values in these tables:

- ``ticket``  ``(id, value)`` — value has title, mandate, assignments[] (with ``report``
  objects), channels, attention, ended, ... These become ``ticket`` records.
- ``trigger`` ``(id, value, at)`` — value has text, actor, human, channel, meta. ``trigger`` records.
- ``decision`` ``(id, thread, value)`` — value has kind, reason[], strength. ``decision`` records.
- ``thread``, ``disposition``, ``outbox``, ``effect``, ``kv`` — not exported directly (context is
  folded into tickets/decisions; ``kv``/``outbox`` may contain credential-adjacent state and are
  skipped).

Implementation notes:

- Open sqlite read-only (``file:...?mode=ro``); never copy the WAL or the database.
- Python's bundled SQLite must be >= 3.38 because the live schema uses JSON operators in
  generated columns (``->>``). :func:`sqlite_version_ok` reports it and :func:`collect` raises
  :class:`SqliteTooOldError` with an actionable message instead of failing deep inside sqlite.
- Incremental: a cursor per bot/table stores an ISO-8601 watermark (``value.updatedAt`` for
  tickets, ``at`` for triggers and decisions). Rows are parsed in Python rather than filtered
  with JSON SQL operators, so the same code runs on any SQLite >= 3.38 and the fixture databases
  used by tests (which do not define the generated columns).
- Idempotent: only rows newer than the watermark are exported; older records are never deleted.
- Redaction: title/summary/payload/refs go through :class:`~context_bridge.redact.Redactor`;
  masks are counted into ``Record.redactions``.
"""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from ..model import BotSpec, Record
from ..redact import Redactor

LOG = logging.getLogger(__name__)

#: File name of the superagent state database inside ``BotSpec.state_dir``.
ORCHESTRATOR_DB = "orchestrator.sqlite"

#: JSON operators in generated columns require SQLite >= 3.38 (2022-02-22).
MIN_SQLITE_VERSION = (3, 38)

#: Tables exported as records, in collection order.
EXPORTED_TABLES = ("ticket", "trigger", "decision")

_TITLE_MAX = 300
_SUMMARY_MAX = 2000
_REASON_MAX = 600


class SqliteTooOldError(RuntimeError):
    """The interpreter's SQLite is too old to read a live superagent state database."""


def sqlite_version_ok() -> bool:
    """True when the interpreter's bundled SQLite can read the live schema (>= 3.38)."""
    return tuple(sqlite3.sqlite_version_info) >= MIN_SQLITE_VERSION


def _too_old_message() -> str:
    return (
        "this superagent state database uses SQLite generated columns with JSON operators "
        "(->>), which requires SQLite >= 3.38; the current interpreter has SQLite "
        f"{sqlite3.sqlite_version}. Run the collector with a Python built against a newer "
        "SQLite (for example a conda/miniforge python; check with "
        '`python -c "import sqlite3; print(sqlite3.sqlite_version)"`), or point the deployment '
        "at such an interpreter."
    )


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _schema_needs_newer_sqlite(con: sqlite3.Connection) -> bool:
    """True when a table definition needs the ``->>`` JSON operator (SQLite >= 3.38)."""
    return any(
        "->>" in (row[0] or "")
        for row in con.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
    )


def _ensure_sqlite_compatible(db_path: Path) -> None:
    """Raise :class:`SqliteTooOldError` when the DB cannot be read, but let fixture DBs through."""
    if sqlite_version_ok():
        return
    try:
        with _connect(db_path) as con:
            needs_newer = _schema_needs_newer_sqlite(con)
    except sqlite3.DatabaseError as exc:  # pragma: no cover - only on old interpreters
        raise SqliteTooOldError(_too_old_message()) from exc
    if needs_newer:  # pragma: no cover - only on old interpreters with live DBs
        raise SqliteTooOldError(_too_old_message())


# -- value helpers -------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _state_open(value: dict[str, Any]) -> str:
    return "ended" if value.get("ended") else "open"


def _active(value: dict[str, Any]) -> bool:
    if _as_list(value.get("undelivered")):
        return True
    assignments = _as_list(value.get("assignments"))
    if assignments and isinstance(assignments[-1], dict):
        return assignments[-1].get("state") in ("pending", "running")
    return False


def _latest_report(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    assignments = _as_list(value.get("assignments"))
    for assignment in reversed(assignments):
        if isinstance(assignment, dict) and isinstance(assignment.get("report"), dict):
            return assignment["report"], assignment
    last = assignments[-1] if assignments and isinstance(assignments[-1], dict) else {}
    return {}, last


def _mandate_payload(value: dict[str, Any]) -> dict[str, Any]:
    mandate = _as_dict(value.get("mandate"))
    basis = _as_dict(mandate.get("basis"))
    actor = _as_dict(mandate.get("actor"))
    return {
        "strength": mandate.get("strength"),
        "scope": mandate.get("scope"),
        "actor": {"source": actor.get("source"), "id": actor.get("id")},
        "basis": {
            "source": basis.get("source"),
            "row": basis.get("row"),
            "sender": basis.get("sender"),
            "via": basis.get("via"),
            "addressed": basis.get("addressed"),
        },
    }


def _decision_reasons(value: dict[str, Any]) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    for reason in _as_list(value.get("reason")):
        if not isinstance(reason, dict):
            if isinstance(reason, str):
                reasons.append({"code": _truncate(reason, _REASON_MAX), "text": ""})
            continue
        values = _as_dict(reason.get("values"))
        reasons.append(
            {
                "code": reason.get("code"),
                "text": _truncate(str(values.get("text") or ""), _REASON_MAX),
                "trigger_id": reason.get("triggerId"),
            }
        )
    return reasons


# -- record builders -----------------------------------------------------------------


def _make_record(
    redactor: Redactor,
    *,
    bot: str,
    kind: str,
    source_id: str,
    title: str,
    summary: str,
    created_at: str,
    updated_at: str,
    tags: list[str],
    refs: dict[str, Any],
    source: dict[str, Any],
    payload: dict[str, Any],
) -> Record:
    title_result = redactor.clean(_one_line(_truncate(title, _TITLE_MAX)))
    summary_result = redactor.clean(_truncate(summary, _SUMMARY_MAX))
    title_clean, n_title = title_result.text, title_result.count
    summary_clean, n_summary = summary_result.text, summary_result.count
    payload_clean, n_payload = redactor.clean_obj(payload)
    refs_clean, n_refs = redactor.clean_obj(refs)
    tags_clean, n_tags = redactor.clean_obj(tags)
    return Record(
        bot=bot,
        kind=kind,
        title=title_clean or f"{kind} {source_id}",
        summary=summary_clean,
        created_at=created_at,
        updated_at=updated_at or created_at,
        tags=list(tags_clean or []),
        refs=dict(refs_clean or {}),
        source=source,
        payload=dict(payload_clean or {}),
        redactions=n_title + n_summary + n_payload + n_refs + n_tags,
    )


def _source(bot_spec: BotSpec, db_path: Path, kind: str, source_id: str) -> dict[str, Any]:
    return {
        "system": "dsh-superagent",
        "profile": bot_spec.profile,
        "path": str(db_path),
        "source_id": source_id,
        "table": kind,
    }


def _ticket_record(
    redactor: Redactor, bot_spec: BotSpec, db_path: Path, row_id: str, value: dict[str, Any]
) -> Record:
    report, last_assignment = _latest_report(value)
    latest = {
        "state": report.get("state") or (last_assignment.get("state") if last_assignment else None),
        "summary": _truncate(str(report.get("summary") or ""), _SUMMARY_MAX),
        "at": report.get("at"),
    }
    attention = value.get("attention")
    attention_payload = None
    if isinstance(attention, dict):
        attention_payload = {
            "id": attention.get("id"),
            "question": _truncate(str(attention.get("question") or ""), _SUMMARY_MAX),
            "trigger_id": attention.get("triggerId") or attention.get("trigger_id"),
        }
    state = _state_open(value)
    summary_parts: list[str] = []
    if attention_payload and attention_payload.get("question"):
        summary_parts.append(f"⚠ attention: {attention_payload['question']}")
    if latest["summary"]:
        label = latest["state"] or "report"
        summary_parts.append(f"latest report ({label}): {latest['summary']}")
    if not summary_parts and value.get("ended"):
        ended = _as_dict(value.get("ended"))
        summary_parts.append(f"ended ({ended.get('outcome') or ended.get('by') or 'unknown'})")
    channels = []
    for channel in _as_list(value.get("channels"))[:3]:
        if isinstance(channel, dict):
            channels.append({"source": channel.get("source"), "thread": channel.get("thread")})
    assignments = [a for a in _as_list(value.get("assignments")) if isinstance(a, dict)]
    recent_reports: list[dict[str, Any]] = []
    for assignment in assignments[-3:]:
        report = assignment.get("report")
        if not isinstance(report, dict):
            continue
        recent_reports.append(
            {
                "state": report.get("state") or assignment.get("state"),
                "summary": _truncate(str(report.get("summary") or ""), _SUMMARY_MAX),
                "at": report.get("at"),
            }
        )
    tags = ["superagent", state]
    if attention_payload:
        tags.append("attention")
    if state == "ended":
        ended = _as_dict(value.get("ended"))
        if ended.get("outcome"):
            tags.append(str(ended["outcome"]))
    mandate = _mandate_payload(value)
    payload = {
        "state": state,
        "active": _active(value),
        "attention": attention_payload,
        "latest_report": latest,
        "reports": recent_reports,
        "assignment_count": len(assignments),
        "assignment_states": sorted({str(a.get("state")) for a in assignments if a.get("state")}),
        "channels": channels,
        "mandate": mandate,
        "origin": _as_dict(value.get("origin")).get("triggerId"),
        "ended": _as_dict(value.get("ended")) or None,
    }
    refs: dict[str, Any] = {"ticket": row_id}
    if value.get("decisionId"):
        refs["decision"] = value["decisionId"]
    if value.get("origin"):
        refs["trigger"] = _as_dict(value.get("origin")).get("triggerId")
    if channels and channels[0].get("thread"):
        refs["thread"] = channels[0]["thread"]
    title = _first_str(value.get("title"), mandate.get("scope"), f"ticket {row_id}")
    return _make_record(
        redactor,
        bot=bot_spec.id,
        kind="ticket",
        source_id=row_id,
        title=title,
        summary="\n".join(summary_parts),
        created_at=str(value.get("createdAt") or ""),
        updated_at=str(value.get("updatedAt") or value.get("createdAt") or ""),
        tags=tags,
        refs=refs,
        source=_source(bot_spec, db_path, "ticket", row_id),
        payload=payload,
    )


def _trigger_record(
    redactor: Redactor, bot_spec: BotSpec, db_path: Path, row_id: str, at: str, value: dict[str, Any]
) -> Record:
    text = str(value.get("text") or "")
    actor = _as_dict(value.get("actor"))
    channel = _as_dict(value.get("channel"))
    addressed = value.get("addressed")
    human = bool(value.get("human"))
    tags = ["superagent", "human" if human else "agent"]
    if isinstance(addressed, str) and addressed:
        tags.append(addressed)
    refs: dict[str, Any] = {"trigger": row_id}
    threads = _as_list(value.get("threads"))
    if threads and isinstance(threads[0], dict) and threads[0].get("id"):
        refs["thread"] = threads[0]["id"]
    payload = {
        "human": human,
        "addressed": addressed,
        "actor": {"source": actor.get("source"), "id": actor.get("id")},
        "channel": {"source": channel.get("source"), "thread": channel.get("thread")},
        "source": value.get("source"),
    }
    return _make_record(
        redactor,
        bot=bot_spec.id,
        kind="trigger",
        source_id=row_id,
        title=_first_str(_one_line(text), f"trigger {row_id}"),
        summary=text,
        created_at=str(value.get("at") or at or ""),
        updated_at=str(value.get("at") or at or ""),
        tags=tags,
        refs=refs,
        source=_source(bot_spec, db_path, "trigger", row_id),
        payload=payload,
    )


def _decision_record(
    redactor: Redactor,
    bot_spec: BotSpec,
    db_path: Path,
    row_id: str,
    thread: str,
    value: dict[str, Any],
) -> Record:
    kind = str(value.get("kind") or "decision")
    strength = value.get("strength")
    reasons = _decision_reasons(value)
    reason_text = "; ".join(
        f"{r.get('code')}: {r.get('text')}" if r.get("text") else str(r.get("code") or "")
        for r in reasons
        if r.get("code") or r.get("text")
    )
    tags = ["superagent", kind]
    if strength:
        tags.append(str(strength))
    payload = {
        "kind": kind,
        "strength": strength,
        "reasons": reasons,
        "eval_id": value.get("evalId"),
        "policy_version": value.get("policyVersion"),
        "thread": value.get("threadKey") or thread,
        "trigger_ids": _as_list(value.get("triggerIds"))[:20],
    }
    refs: dict[str, Any] = {"decision": row_id}
    thread_key = value.get("threadKey") or thread
    if thread_key:
        refs["thread"] = thread_key
    at = str(value.get("at") or "")
    return _make_record(
        redactor,
        bot=bot_spec.id,
        kind="decision",
        source_id=row_id,
        title=f"{kind}: {reason_text}" if reason_text else f"decision {kind}",
        summary=reason_text,
        created_at=at,
        updated_at=at,
        tags=tags,
        refs=refs,
        source=_source(bot_spec, db_path, "decision", row_id),
        payload=payload,
    )


# -- cursor-aware reading ------------------------------------------------------------


def _read_table(con: sqlite3.Connection, table: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Return ``(row_id, at, value)`` tuples for one table."""
    rows: list[tuple[str, str, dict[str, Any]]] = []
    if table == "trigger":
        cursor = con.execute("SELECT id, value, at FROM trigger")
        for row_id, raw, at in cursor:
            rows.append((str(row_id), str(at or ""), _loads(raw)))
    elif table == "decision":
        cursor = con.execute("SELECT id, thread, value FROM decision")
        for row_id, thread, raw in cursor:
            value = _loads(raw)
            rows.append((str(row_id), str(value.get("at") or thread or ""), value))
    else:
        cursor = con.execute("SELECT id, value FROM ticket")
        for row_id, raw in cursor:
            value = _loads(raw)
            rows.append((str(row_id), str(value.get("updatedAt") or value.get("createdAt") or ""), value))
    return rows


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _row_ts(bot_cursor: dict[str, Any], table: str) -> str:
    entry = bot_cursor.get(table)
    return str(entry.get("watermark") or "") if isinstance(entry, dict) else ""


def _set_watermark(bot_cursor: dict[str, Any], table: str, watermark: str) -> None:
    entry = bot_cursor.setdefault(table, {})
    current = str(entry.get("watermark") or "")
    if watermark and watermark > current:
        entry["watermark"] = watermark


def collect(config: Any, cursors: dict, redactor: Redactor) -> tuple[list[Record], dict]:
    """Export ticket/trigger/decision records for every configured bot with a state DB.

    Returns ``(records, new_cursors)``; ``new_cursors`` is a full copy of ``cursors`` with the
    ``"superagent"`` section replaced, so it composes with other collectors.
    """
    new_cursors = copy.deepcopy(cursors or {})
    state = new_cursors.setdefault("superagent", {})
    if not isinstance(state, dict):
        state = {}
        new_cursors["superagent"] = state
    records: list[Record] = []
    for bot_id, bot_spec in sorted(config.bots.items()):
        db_path = Path(bot_spec.state_dir) / ORCHESTRATOR_DB
        bot_cursor = state.setdefault(bot_id, {})
        if not isinstance(bot_cursor, dict):
            bot_cursor = {}
            state[bot_id] = bot_cursor
        if not db_path.is_file():
            continue
        try:
            _ensure_sqlite_compatible(db_path)
            con = _connect(db_path)
        except SqliteTooOldError:
            raise
        except sqlite3.DatabaseError as exc:
            LOG.warning("superagent collector: cannot open %s: %s", db_path, exc)
            continue
        try:
            for table in EXPORTED_TABLES:
                watermark = _row_ts(bot_cursor, table)
                try:
                    rows = _read_table(con, table)
                except sqlite3.DatabaseError as exc:
                    LOG.warning("superagent collector: cannot read %s.%s: %s", db_path, table, exc)
                    continue
                max_ts = watermark
                for row_id, at, value in rows:
                    if watermark and at and at <= watermark:
                        continue
                    if not value:
                        continue
                    if table == "ticket":
                        record = _ticket_record(redactor, bot_spec, db_path, row_id, value)
                    elif table == "trigger":
                        record = _trigger_record(redactor, bot_spec, db_path, row_id, at, value)
                    else:
                        thread = _first_str(value.get("threadKey"))
                        record = _decision_record(
                            redactor, bot_spec, db_path, row_id, thread, value
                        )
                    records.append(record)
                    if at and at > max_ts:
                        max_ts = at
                _set_watermark(bot_cursor, table, max_ts)
        finally:
            con.close()
    return records, new_cursors
