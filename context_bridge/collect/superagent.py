"""Collector for ``dsh-superagent`` bots.

Each bot's state directory holds ``orchestrator.sqlite`` with JSON values in these tables:

- ``ticket``  ``(id, value, open, active)`` — value has title, mandate, assignments[] (with
  ``report`` objects), channels, attention, ended, ... These become ``ticket`` records.
- ``trigger`` ``(id, value, at)`` — value has text, actor, human, channel, meta. ``trigger`` records.
- ``decision`` ``(id, thread, value)`` — value has kind, reason[], strength. ``decision`` records.
- ``thread``, ``disposition``, ``outbox``, ``kv`` — not exported directly (context is folded into
  tickets/decisions; ``kv`` may contain credentials-adjacent state and is skipped).

Implementation notes for the owner:

- Open sqlite read-only: ``sqlite3.connect(f"file:{path}?mode=ro", uri=True)``. Python's bundled
  SQLite must be >= 3.38 because the schema uses JSON operators in generated columns (``->>``).
  Detect this and raise an actionable message; the host deployment may need a newer interpreter.
- Read the WAL properly (read-only URI is enough; do not copy the file).
- Incremental: cursor stores ``updated_at`` watermark and/or row ids per table.
- Map summaries from the latest ``assignments[].report`` (state + summary) and ``attention``.
- Redact title/summary/payload text through :class:`Redactor`; count masks into ``Record.redactions``.
- Idempotent: rewrites only records whose source row changed; never loses older records.
"""

from __future__ import annotations


def sqlite_version_ok() -> bool:
    raise NotImplementedError("collect.superagent.sqlite_version_ok is implemented by the data-layer owner")


def collect(config, cursors: dict, redactor) -> tuple[list, dict]:
    raise NotImplementedError("collect.superagent.collect is implemented by the data-layer owner")
