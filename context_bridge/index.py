"""Full-text search index over hub records.

A rebuildable cache: ``<hub>/index/search.sqlite`` with an FTS5 table. If the index is missing or
stale the server rebuilds it (or falls back to a linear scan for small hubs). SQLite FTS5 is part
of the Python standard library build on supported platforms.

    idx = SearchIndex(hub)
    idx.rebuild() -> int
    idx.search(q, bot=None, kind=None, limit=30) -> list[dict]  # {"record": Record, "snippet": str}
    idx.is_stale() -> bool

The whole index is a cache: :meth:`SearchIndex.rebuild` builds a fresh database next to the old
one and atomically replaces it, so a reader never observes a half-built index. When FTS5 is not
available at all (some exotic SQLite builds), search degrades to an in-memory linear scan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .model import Record

log = logging.getLogger(__name__)

SNIPPET_OPEN = "<b>"
SNIPPET_CLOSE = "</b>"
_MEANINGFUL = re.compile(r"[0-9A-Za-z_\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")

_FTS5_SUPPORTED: bool | None = None


def fts5_supported() -> bool:
    """Whether the linked SQLite build exposes the FTS5 module (cached)."""
    global _FTS5_SUPPORTED
    if _FTS5_SUPPORTED is None:
        try:
            con = sqlite3.connect(":memory:")
            try:
                con.execute("CREATE VIRTUAL TABLE _ctxbridge_probe USING fts5(x)")
            finally:
                con.close()
            _FTS5_SUPPORTED = True
        except sqlite3.Error:
            _FTS5_SUPPORTED = False
    return _FTS5_SUPPORTED


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF  # kana
        or 0x3400 <= code <= 0x4DBF  # CJK ext A
        or 0x4E00 <= code <= 0x9FFF  # CJK unified
        or 0xF900 <= code <= 0xFAFF  # CJK compatibility
        or 0xAC00 <= code <= 0xD7AF  # hangul
    )


def normalize_for_fts(text: str) -> str:
    """Insert spaces around CJK characters so a CJK query can match a longer CJK run.

    ``unicode61`` tokenises a run of CJK characters as one long token; spacing each character
    makes substring-style Chinese lookups work at the cost of exact phrase matching.
    """
    pieces: list[str] = []
    for char in str(text or ""):
        if _is_cjk(char):
            pieces.append(" ")
            pieces.append(char)
            pieces.append(" ")
        else:
            pieces.append(char)
    return " ".join("".join(pieces).split())


def _tokens_for_query(query: str) -> list[str]:
    tokens = []
    for token in normalize_for_fts(query).split():
        if _MEANINGFUL.search(token):
            tokens.append(token.replace('"', ""))
    return tokens


def _haystack(record: Record) -> str:
    payload = ""
    try:
        payload = json.dumps(record.payload, ensure_ascii=False)
    except (TypeError, ValueError):
        payload = str(record.payload)
    return "\n".join(
        [
            record.title,
            record.summary,
            " ".join(record.tags),
            payload[:4000],
        ]
    )


def _make_snippet(text: str, tokens: list[str], context: int = 60, length: int = 240) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    lower = text.casefold()
    position = -1
    for token in sorted(tokens, key=len, reverse=True):
        position = lower.find(token.casefold())
        if position >= 0:
            break
    if position < 0:
        return text[:length] + ("…" if len(text) > length else "")
    start = max(0, position - context)
    end = min(len(text), start + length)
    window = text[start:end]
    if tokens:
        pattern = "|".join(re.escape(token) for token in sorted(tokens, key=len, reverse=True))
        try:
            window = re.sub(
                pattern,
                lambda match: f"{SNIPPET_OPEN}{match.group(0)}{SNIPPET_CLOSE}",
                window,
                flags=re.IGNORECASE,
            )
        except re.error:
            pass
    return ("…" if start > 0 else "") + window + ("…" if end < len(text) else "")


def _snippet_for_record(record: Record, tokens: list[str]) -> str:
    """Best snippet: prefer the field that actually contains one of the query tokens."""
    for candidate in (record.summary, record.title, " ".join(record.tags)):
        if candidate and any(token.casefold() in candidate.casefold() for token in tokens):
            return _make_snippet(candidate, tokens)
    return _make_snippet(record.summary or record.title, tokens)


class SearchIndex:
    def __init__(self, hub, use_fts: bool | None = None) -> None:
        self.hub = hub
        self.path: Path = hub.index_dir / "search.sqlite"
        self.use_fts = fts5_supported() if use_fts is None else bool(use_fts)

    # -- build ---------------------------------------------------------------------
    def rebuild(self) -> int:
        """Rebuild the index from the hub's records. Returns the number of indexed records."""
        index_dir = self.hub.index_dir
        index_dir.mkdir(parents=True, exist_ok=True)
        if not self.use_fts:
            log.warning("SQLite FTS5 is unavailable; search will use the linear scan fallback")
            return 0

        tmp = index_dir / f".search.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp.sqlite"
        try:
            con = sqlite3.connect(str(tmp))
            try:
                self._create_schema(con)
                count = 0
                max_mtime = 0.0
                for path, record in self._iter_record_files():
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    max_mtime = max(max_mtime, mtime)
                    self._insert(con, record)
                    count += 1
                con.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    [
                        ("built_at", _utcnow()),
                        ("records_count", str(count)),
                        ("max_mtime", repr(max_mtime)),
                        ("schema", "1"),
                    ],
                )
                con.commit()
            finally:
                con.close()
            os.replace(tmp, self.path)
            return count
        except sqlite3.OperationalError as exc:
            _safe_unlink(tmp)
            if "fts5" in str(exc).lower():
                global _FTS5_SUPPORTED
                _FTS5_SUPPORTED = False
                self.use_fts = False
                log.warning("SQLite FTS5 is unavailable; search will use the linear scan fallback")
                return 0
            raise
        except BaseException:
            _safe_unlink(tmp)
            raise

    def _create_schema(self, con: sqlite3.Connection) -> None:
        con.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE records_fts USING fts5(
                record_id UNINDEXED,
                bot UNINDEXED,
                kind UNINDEXED,
                title,
                summary,
                tags,
                body,
                tokenize = 'unicode61 remove_diacritics 2'
            );
            """
        )

    def _insert(self, con: sqlite3.Connection, record: Record) -> None:
        body = _haystack(record)
        con.execute(
            "INSERT INTO records_fts(record_id, bot, kind, title, summary, tags, body)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.bot,
                record.kind,
                normalize_for_fts(record.title),
                normalize_for_fts(record.summary),
                normalize_for_fts(" ".join(record.tags)),
                normalize_for_fts(body),
            ),
        )

    def _iter_record_files(self):
        """Yield ``(path, Record)`` pairs from the hub, skipping unreadable files."""
        if not self.hub.records_dir.is_dir():
            return
        for path in sorted(self.hub.records_dir.rglob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                record = Record.from_dict(data)
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
                AttributeError,
            ):
                continue
            yield path, record

    # -- search --------------------------------------------------------------------
    def search(
        self,
        query: str,
        bot: str | None = None,
        kind: str | None = None,
        limit: int = 30,
    ) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(limit, 200))
        if self.use_fts:
            if not self.path.is_file() or self.is_stale():
                self.rebuild()
            hits = self._fts_search(query, bot, kind, limit)
            if hits is not None:
                return hits
            self.use_fts = False
        return self._linear_search(query, bot, kind, limit)

    def _fts_search(self, query, bot, kind, limit) -> list[dict] | None:
        expression = " AND ".join(f'"{token}"' for token in _tokens_for_query(query))
        if not expression:
            return []
        sql = (
            "SELECT record_id, bm25(records_fts) AS rank,"
            " snippet(records_fts, 4, ?, ?, '…', 24) AS summary_snippet,"
            " snippet(records_fts, 6, ?, ?, '…', 24) AS body_snippet"
            " FROM records_fts WHERE records_fts MATCH ?"
        )
        params: list[Any] = [SNIPPET_OPEN, SNIPPET_CLOSE, SNIPPET_OPEN, SNIPPET_CLOSE, expression]
        if bot:
            sql += " AND bot = ?"
            params.append(bot)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        tokens = _tokens_for_query(query)
        hits: list[dict] = []
        try:
            con = sqlite3.connect(str(self.path))
            try:
                rows = con.execute(sql, params).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        for record_id, rank, summary_snippet, body_snippet in rows:
            record = self.hub.read_record(record_id)
            if record is None:
                continue
            snippet = (summary_snippet or "").strip()
            if SNIPPET_OPEN not in snippet:
                snippet = (body_snippet or "").strip()
            if SNIPPET_OPEN not in snippet:
                snippet = _snippet_for_record(record, tokens)
            score = 1.0 if not rank else round(1.0 / (1.0 + abs(float(rank))), 4)
            hits.append({"record": record, "snippet": snippet, "score": score})
        return hits

    def _linear_search(self, query, bot, kind, limit) -> list[dict]:
        tokens = _tokens_for_query(query)
        if not tokens:
            return []
        folded = [token.casefold() for token in tokens]
        matches: list[tuple[Record, int]] = []
        for record in self.hub.iter_records(bot=bot, kind=kind):
            haystack = _haystack(record).casefold()
            if all(token in haystack for token in folded):
                matches.append((record, haystack.count(folded[0])))
        matches.sort(key=lambda item: (item[1], item[0].updated_at or item[0].created_at), reverse=True)
        hits = []
        for record, occurrences in matches[:limit]:
            hits.append(
                {
                    "record": record,
                    "snippet": _snippet_for_record(record, tokens),
                    "score": min(1.0, 0.5 + 0.1 * occurrences),
                }
            )
        return hits

    # -- staleness -----------------------------------------------------------------
    def is_stale(self) -> bool:
        """True when the index is missing or older than the records it should cover."""
        if not self.path.is_file():
            return True
        meta = self._read_meta()
        if meta is None:
            return True
        count = 0
        max_mtime = 0.0
        if self.hub.records_dir.is_dir():
            for path in self.hub.records_dir.rglob("*.json"):
                count += 1
                try:
                    max_mtime = max(max_mtime, path.stat().st_mtime)
                except OSError:
                    continue
        try:
            indexed_count = int(meta.get("records_count", "-1"))
            indexed_mtime = float(meta.get("max_mtime", "0") or 0.0)
        except (TypeError, ValueError):
            return True
        return count != indexed_count or max_mtime > indexed_mtime + 1e-6

    def _read_meta(self) -> dict[str, str] | None:
        try:
            con = sqlite3.connect(str(self.path))
            try:
                rows = con.execute("SELECT key, value FROM meta").fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        return {str(key): str(value) for key, value in rows}


def _utcnow() -> str:
    from .model import utcnow_iso

    return utcnow_iso()


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
