"""Deterministic daily digests.

The digest is a person- and bot-readable Markdown summary of one bot's day, generated only from
hub records (so it inherits redaction and needs no model call): tickets opened/closed, decisions,
notable session outcomes, notes. Written to ``digests/<bot>/<YYYY-MM-DD>.md``.

Contract (implemented by the data-layer owner):

    build_digest(bot, records, date) -> str
    build_and_write_digests(hub, bots, date=None) -> dict[str, Path]

Determinism: output depends only on the records and the date — no wall-clock time, no dict
ordering leaks — so a re-sync that changes nothing rewrites byte-identical digests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# Section output caps keep one digest readable; the counts line still reports the full total.
_LIMITS = {"ticket": 30, "decision": 20, "session": 15, "note": 20, "trigger": 0}
_SECTION_TITLES = [
    ("ticket", "Tickets"),
    ("decision", "Decisions"),
    ("session", "Sessions"),
    ("note", "Notes"),
    ("trigger", "Triggers"),
]


def _field(record: Any, name: str, default: Any = "") -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _records_by_kind(bot: str, records: Iterable[Any]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for record in records:
        if _field(record, "bot", bot) != bot:
            continue
        grouped.setdefault(str(_field(record, "kind", "note")), []).append(record)
    for items in grouped.values():
        items.sort(key=lambda r: (str(_field(r, "updated_at")), str(_field(r, "id"))), reverse=True)
    return grouped


def _on_date(record: Any, date: str) -> bool:
    return str(_field(record, "created_at", "")).startswith(date) or str(
        _field(record, "updated_at", "")
    ).startswith(date)


def _summary_line(record: Any, limit: int = 300) -> str:
    text = " ".join(str(_field(record, "summary", "") or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _ticket_line(record: Any) -> str:
    payload = _field(record, "payload", {}) or {}
    state = payload.get("state") if isinstance(payload, Mapping) else ""
    attention = payload.get("attention") if isinstance(payload, Mapping) else None
    line = f"- **{_field(record, 'title')}**"
    if state:
        line += f" · {state}"
    if isinstance(attention, Mapping) and attention.get("question"):
        line += f" · ⚠ {_summary_line({'summary': attention.get('question')}, limit=160)}"
    summary = _summary_line(record)
    if summary:
        line += f" — {summary}"
    return line


def _decision_line(record: Any) -> str:
    payload = _field(record, "payload", {}) or {}
    kind = payload.get("kind") if isinstance(payload, Mapping) else ""
    strength = payload.get("strength") if isinstance(payload, Mapping) else ""
    line = f"- **{_field(record, 'title')}**"
    summary = _summary_line(record)
    if summary and summary not in str(_field(record, "title")):
        line += f" — {summary}"
    if strength and strength not in line:
        line += f" ({strength})"
    return line


def _session_line(record: Any) -> str:
    summary = _summary_line(record, limit=400)
    return f"- **{_field(record, 'title')}**" + (f" — {summary}" if summary else "")


def _note_line(record: Any) -> str:
    summary = _summary_line(record)
    return f"- **{_field(record, 'title')}**" + (f": {summary}" if summary else "")


def _trigger_line(record: Any) -> str:
    return f"- {_summary_line(record, limit=200)}"


def _format_record(kind: str, record: Any) -> str:
    if kind == "ticket":
        return _ticket_line(record)
    if kind == "decision":
        return _decision_line(record)
    if kind == "session":
        return _session_line(record)
    if kind == "trigger":
        return _trigger_line(record)
    return _note_line(record)


def build_digest(bot: str, records: Iterable[Any], date: str) -> str:
    """Render the Markdown digest for one bot and one ISO date (``YYYY-MM-DD``)."""
    grouped = _records_by_kind(bot, records)
    day_counts = {kind: sum(1 for r in items if _on_date(r, date)) for kind, items in grouped.items()}
    day_records = {
        kind: [r for r in items if _on_date(r, date)] for kind, items in grouped.items()
    }
    total = sum(day_counts.values())
    lines = [f"# {bot} — daily digest {date}", ""]
    tickets = day_counts.get("ticket", 0)
    if total:
        lines.append(
            f"_{_plural(total, 'hub record')} on {date}: "
            f"{_plural(tickets, 'ticket')}, "
            f"{_plural(day_counts.get('decision', 0), 'decision')}, "
            f"{_plural(day_counts.get('session', 0), 'session')}, "
            f"{_plural(day_counts.get('note', 0), 'note')}._"
        )
    else:
        lines.append(f"_No hub records dated {date}._")
    lines.append("")
    for kind, section_title in _SECTION_TITLES:
        items = day_records.get(kind) or []
        limit = _LIMITS.get(kind, 10)
        if not items or limit <= 0:
            continue
        lines.append(f"## {section_title} ({len(items)})")
        lines.append("")
        for record in items[:limit]:
            lines.append(_format_record(kind, record))
        if len(items) > limit:
            lines.append(f"- _… and {len(items) - limit} more_")
        lines.append("")
    if total:
        lines.append("_Generated deterministically from redacted hub records._")
        lines.append("")
    return "\n".join(lines)


def _iter_bots(hub: Any, bots: Any) -> dict[str, Any]:
    if bots is None:
        config = getattr(hub, "config", None)
        bots = getattr(config, "bots", None) or {}
    if isinstance(bots, Mapping):
        return dict(bots)
    resolved: dict[str, Any] = {}
    for item in bots or []:
        if isinstance(item, str):
            resolved[item] = None
        elif hasattr(item, "id"):
            resolved[str(item.id)] = item
    return resolved


def build_and_write_digests(hub: Any, bots: Any, date: str | None = None) -> dict[str, Path]:
    """Build and persist one digest per bot through ``hub.write_digest``; returns ``{bot: path}``."""
    target_date = date or datetime.now(timezone.utc).date().isoformat()
    written: dict[str, Path] = {}
    for bot_id in sorted(_iter_bots(hub, bots)):
        records = list(hub.iter_records(bot=bot_id))
        markdown = build_digest(bot_id, records, target_date)
        written[bot_id] = hub.write_digest(bot_id, target_date, markdown)
    return written


__all__ = ["build_digest", "build_and_write_digests"]
