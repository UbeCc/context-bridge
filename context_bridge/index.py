"""Full-text search index over hub records.

A rebuildable cache: ``<hub>/index/search.sqlite`` with an FTS5 table. If the index is missing or
stale the server rebuilds it (or falls back to a linear scan for small hubs). SQLite FTS5 is part
of the Python standard library build on supported platforms.

Contract (implemented by the hub/CLI owner):

    idx = SearchIndex(hub)
    idx.rebuild() -> int
    idx.search(q, bot=None, kind=None, limit=30) -> list[dict]  # {"record": Record, "snippet": str}
    idx.is_stale() -> bool
"""

from __future__ import annotations


class SearchIndex:
    def __init__(self, hub) -> None:
        self.hub = hub

    def rebuild(self) -> int:
        raise NotImplementedError("SearchIndex.rebuild is implemented by the hub/CLI owner")

    def search(self, query: str, bot: str | None = None, kind: str | None = None, limit: int = 30) -> list[dict]:
        raise NotImplementedError("SearchIndex.search is implemented by the hub/CLI owner")

    def is_stale(self) -> bool:
        raise NotImplementedError("SearchIndex.is_stale is implemented by the hub/CLI owner")
