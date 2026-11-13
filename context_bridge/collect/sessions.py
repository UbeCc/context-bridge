"""Collector for DSH session transcripts.

DSH stores one session per directory under ``<sessions_root>/--<cwd-slug>--/<session-id>/`` as a
zstd-compressed JSONL stream::

    session.v4.jsonl.zstd

The first line is a ``{"type": "session", "id", "createdAt", "cwd", ...}`` header; later lines are
events (``session/title``, ``agent/inbox/spliced`` with the originating request, ``turn/*``,
assistant messages, tool calls, ...). The bridge never copies transcripts. It produces a
``session`` record per session: title, originating request (truncated), outcome summary and
tool-activity counts.

Implementation notes for the owner:

- Decompress with the ``zstd`` binary from PATH (``zstandard`` is not a dependency); fall back to
  the host's ``zstd`` if present at a known path configured via env.
- Attribute sessions to bots by matching the session directory's cwd slug against each bot's
  ``state_dir`` (e.g. ``/srv/bots/opus/state/work/<ticket>`` belongs to bot ``opus``).
- Only digest sessions newer than a configurable window (default 30 days) or changed since the
  cursor (file mtime + size) so a sync loop stays cheap.
- Tolerate corrupt/truncated streams: skip unparsable lines, never crash a whole sync.
- Extract at most a few KB per session; redact everything through :class:`Redactor`.
"""

from __future__ import annotations


def collect(config, cursors: dict, redactor) -> tuple[list, dict]:
    raise NotImplementedError("collect.sessions.collect is implemented by the data-layer owner")
