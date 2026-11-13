"""Shared kanban board stored as ``<hub>/board/board.json``.

Concurrency: every mutation takes the hub lock, re-reads the file, applies one change and writes
atomically. Cards are plain data (see :class:`context_bridge.model.BoardCard`).

Contract (implemented by the hub/CLI owner):

    board = Board(hub)
    board.load() -> {"columns": [...], "cards": [BoardCard, ...]}
    board.add_card(title=..., desc=..., column=..., assignee=..., tags=..., author=...) -> BoardCard
    board.update_card(card_id, **changes) -> BoardCard
    board.delete_card(card_id) -> bool
"""

from __future__ import annotations


class Board:
    def __init__(self, hub) -> None:
        self.hub = hub

    def load(self) -> dict:
        raise NotImplementedError("Board.load is implemented by the hub/CLI owner")

    def add_card(self, **kwargs):
        raise NotImplementedError("Board.add_card is implemented by the hub/CLI owner")

    def update_card(self, card_id: str, **changes):
        raise NotImplementedError("Board.update_card is implemented by the hub/CLI owner")

    def delete_card(self, card_id: str) -> bool:
        raise NotImplementedError("Board.delete_card is implemented by the hub/CLI owner")
