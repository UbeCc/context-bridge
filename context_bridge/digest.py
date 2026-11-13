"""Deterministic daily digests.

The digest is a person- and bot-readable Markdown summary of one bot's day, generated only from
hub records (so it inherits redaction and needs no model call): tickets opened/closed, decisions,
notable session outcomes, notes. Written to ``digests/<bot>/<YYYY-MM-DD>.md``.

Contract (implemented by the data-layer owner):

    build_digest(bot, records, date) -> str
    build_and_write_digests(hub, bots, date=None) -> dict[str, Path]
"""

from __future__ import annotations


def build_digest(bot: str, records, date: str) -> str:
    raise NotImplementedError("digest.build_digest is implemented by the data-layer owner")


def build_and_write_digests(hub, bots, date: str | None = None) -> dict:
    raise NotImplementedError("digest.build_and_write_digests is implemented by the data-layer owner")
