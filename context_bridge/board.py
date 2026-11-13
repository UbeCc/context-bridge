"""Shared kanban board stored as ``<hub>/board/board.json``.

Concurrency: every mutation takes the hub lock, re-reads the file, applies one change and writes
atomically. Cards are plain data (see :class:`context_bridge.model.BoardCard`).

    board = Board(hub)
    board.load() -> {"columns": [...], "cards": [BoardCard, ...]}
    board.add_card(title=..., desc=..., column=..., assignee=..., tags=..., author=...) -> BoardCard
    board.update_card(card_id, **changes) -> BoardCard
    board.delete_card(card_id) -> bool

Mutations commit the hub afterwards (respecting ``hub.commit`` config), so a card change made from
the console or CLI is durable even if no sync run follows.
"""

from __future__ import annotations

import uuid
from typing import Any

from .model import BOARD_COLUMNS, BoardCard, utcnow_iso

_CARD_FIELDS = ("id", "title", "desc", "column", "assignee", "tags", "created_at", "updated_at", "author")
_UPDATABLE = ("title", "desc", "column", "assignee", "tags")


def _default_board() -> dict[str, Any]:
    return {
        "columns": [{"id": col_id, "title": title} for col_id, title in BOARD_COLUMNS],
        "cards": [],
        "updated_at": utcnow_iso(),
    }


def _card_from_raw(raw: dict[str, Any]) -> BoardCard:
    return BoardCard(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        desc=str(raw.get("desc") or ""),
        column=str(raw.get("column") or "inbox"),
        assignee=raw.get("assignee"),
        tags=list(raw.get("tags") or []),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        author=str(raw.get("author") or "human"),
    )


class Board:
    def __init__(self, hub) -> None:
        self.hub = hub
        self.path = hub.board_file

    # -- io ------------------------------------------------------------------------
    def load(self) -> dict:
        data = self._read()
        columns = data.get("columns")
        if not isinstance(columns, list) or not columns:
            columns = _default_board()["columns"]
        cards: list[BoardCard] = []
        for raw in data.get("cards") or []:
            if not isinstance(raw, dict) or not raw.get("title"):
                continue
            try:
                cards.append(_card_from_raw(raw))
            except (TypeError, ValueError):
                continue
        return {"columns": columns, "cards": cards}

    def _read(self) -> dict[str, Any]:
        data = self.hub._read_json(self.path, None)
        if not isinstance(data, dict):
            return _default_board()
        data.setdefault("columns", _default_board()["columns"])
        if not isinstance(data.get("cards"), list):
            data["cards"] = []
        return data

    def _write(self, data: dict[str, Any]) -> None:
        data["updated_at"] = utcnow_iso()
        self.hub.write_json(self.path, data)

    # -- mutations -----------------------------------------------------------------
    def add_card(
        self,
        title: str,
        desc: str = "",
        column: str = "inbox",
        assignee: str | None = None,
        tags: list[str] | None = None,
        author: str = "human",
    ) -> BoardCard:
        card = BoardCard(
            title=(title or "").strip(),
            desc=desc or "",
            column=column,
            assignee=assignee or None,
            tags=list(tags or []),
            author=author or "human",
        )
        if not card.title:
            raise ValueError("card title must not be empty")
        with self.hub.lock():
            data = self._read()
            existing = {raw.get("id") for raw in data["cards"] if isinstance(raw, dict)}
            while card.id in existing:  # the default id has millisecond resolution
                card.id = "c_" + uuid.uuid4().hex[:12]
            data["cards"].append(card.to_dict())
            self._write(data)
        self.hub.commit(f"board: add card {card.id}: {card.title[:60]}")
        return card

    def update_card(self, card_id: str, **changes: Any) -> BoardCard:
        unknown = set(changes) - set(_UPDATABLE)
        if unknown:
            raise ValueError(f"cannot update card fields: {', '.join(sorted(unknown))}")
        if "column" in changes and changes["column"] not in {col for col, _ in BOARD_COLUMNS}:
            raise ValueError(f"unknown board column: {changes['column']!r}")
        if "title" in changes:
            title = str(changes["title"] or "").strip()
            if not title:
                raise ValueError("card title must not be empty")
            changes["title"] = title
        if "tags" in changes:
            changes["tags"] = list(changes["tags"] or [])
        if "assignee" in changes:
            changes["assignee"] = changes["assignee"] or None

        with self.hub.lock():
            data = self._read()
            raw_cards: list[dict[str, Any]] = data.get("cards") or []
            for raw in raw_cards:
                if isinstance(raw, dict) and raw.get("id") == card_id:
                    raw.update(changes)
                    raw["updated_at"] = utcnow_iso()
                    self._write(data)
                    card = _card_from_raw(raw)
                    break
            else:
                raise KeyError(f"board card not found: {card_id}")
        self.hub.commit(f"board: update card {card.id}")
        return card

    def delete_card(self, card_id: str) -> bool:
        with self.hub.lock():
            data = self._read()
            raw_cards = [raw for raw in data.get("cards") or [] if not (isinstance(raw, dict) and raw.get("id") == card_id)]
            if len(raw_cards) == len(data.get("cards") or []):
                return False
            data["cards"] = raw_cards
            self._write(data)
        self.hub.commit(f"board: remove card {card_id}")
        return True
