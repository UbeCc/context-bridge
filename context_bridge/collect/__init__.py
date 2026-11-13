"""Collectors: turn bot-owned state into redacted :class:`context_bridge.model.Record` objects.

One module per source system. Collectors are incremental (cursor-based) and idempotent. They
must never read credential files; only the structured state described in DESIGN.md §5 and
session transcripts.

Contract (implemented by the data-layer owner):

    superagent.collect(config, cursors, redactor) -> (records, new_cursors)
    sessions.collect(config, cursors, redactor) -> (records, new_cursors)
"""
