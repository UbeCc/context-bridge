"""Data model shared by every component.

This module is the frozen contract between collectors, the hub, the index, the API server and
the web console. Keep it dependency-free and stable: anything that serializes to JSON belongs
here, implementation logic does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import SCHEMA_VERSION

# Record kinds. Unknown kinds are allowed on read (forward compatibility) but collectors should
# stick to this list so the console can group them.
KINDS = ("ticket", "decision", "trigger", "session", "note", "command")

# Board columns, in kanban order.
BOARD_COLUMNS = (
    ("inbox", "待处理"),
    ("in_progress", "进行中"),
    ("review", "待验收"),
    ("done", "已完成"),
)
BOARD_COLUMN_IDS = tuple(c[0] for c in BOARD_COLUMNS)

_ID_SAFE = re.compile(r"[^A-Za-z0-9_.:-]+")


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with milliseconds and a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def normalize_id(part: str) -> str:
    """Make one id segment safe for file names and URLs (keeps ``:`` as the separator)."""
    return _ID_SAFE.sub("_", str(part))[:200]


def make_id(bot: str, kind: str, source_id: str) -> str:
    """Stable record id: ``<bot>:<kind>:<source-id>``. One writer per id, by construction."""
    return f"{normalize_id(bot)}:{normalize_id(kind)}:{normalize_id(source_id)}"


@dataclass
class BotSpec:
    """One agent instance the bridge collects from and displays."""

    id: str
    label: str
    profile: str
    state_dir: str
    sessions_root: str = ""
    color: str = "#7c5cff"
    emoji: str = "🤖"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "profile": self.profile,
            "state_dir": self.state_dir,
            "sessions_root": self.sessions_root,
            "color": self.color,
            "emoji": self.emoji,
        }


@dataclass
class Record:
    """One unit of shared memory. See DESIGN.md §5 for the envelope semantics."""

    bot: str
    kind: str
    title: str
    id: str = ""
    summary: str = ""
    created_at: str = ""
    updated_at: str = ""
    tags: list[str] = field(default_factory=list)
    refs: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    redactions: int = 0
    v: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = utcnow_iso()
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.id:
            src = str(self.source.get("source_id", "")) or normalize_id(self.title)[:40]
            self.id = make_id(self.bot, self.kind, src)
        self.title = (self.title or "").strip()
        if len(self.title) > 300:
            self.title = self.title[:297] + "…"

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "id": self.id,
            "bot": self.bot,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": list(self.tags),
            "refs": dict(self.refs),
            "source": dict(self.source),
            "payload": dict(self.payload),
            "redactions": self.redactions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Record":
        return cls(
            v=int(data.get("v", SCHEMA_VERSION)),
            id=str(data.get("id", "")),
            bot=str(data.get("bot", "")),
            kind=str(data.get("kind", "note")),
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            tags=list(data.get("tags") or []),
            refs=dict(data.get("refs") or {}),
            source=dict(data.get("source") or {}),
            payload=dict(data.get("payload") or {}),
            redactions=int(data.get("redactions", 0)),
        )

    def brief(self) -> dict[str, Any]:
        """The compact representation used by list endpoints and the console."""
        return {
            "id": self.id,
            "bot": self.bot,
            "kind": self.kind,
            "title": self.title,
            "summary": self.summary[:600],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": list(self.tags),
        }


def record_relpath(record: Record) -> str:
    """Path of a record inside the hub, relative to the hub root."""
    return f"records/{normalize_id(record.bot)}/{normalize_id(record.kind)}/{normalize_id(record.id)}.json"


@dataclass
class BoardCard:
    title: str
    id: str = ""
    desc: str = ""
    column: str = "inbox"
    assignee: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    author: str = "human"

    def __post_init__(self) -> None:
        if self.column not in BOARD_COLUMN_IDS:
            raise ValueError(f"unknown board column: {self.column!r}")
        if not self.id:
            self.id = "c_" + normalize_id(utcnow_iso().replace("-", "").replace(":", "").replace(".", ""))[-10:]
        if not self.created_at:
            self.created_at = utcnow_iso()
        if not self.updated_at:
            self.updated_at = self.created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "desc": self.desc,
            "column": self.column,
            "assignee": self.assignee,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "author": self.author,
        }


@dataclass
class Command:
    """An instruction relayed by a person to one bot through its chat source."""

    target_bot: str
    text: str
    id: str = ""
    via: str = "feishu"
    status: str = "pending"  # pending | sent | failed | skipped
    message_id: str | None = None
    error: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = "cmd_" + normalize_id(utcnow_iso().replace("-", "").replace(":", "").replace(".", ""))[-12:]
        if not self.created_at:
            self.created_at = utcnow_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target_bot": self.target_bot,
            "text": self.text,
            "via": self.via,
            "status": self.status,
            "message_id": self.message_id,
            "error": self.error,
            "created_at": self.created_at,
        }
