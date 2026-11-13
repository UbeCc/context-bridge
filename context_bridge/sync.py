"""Orchestration: one collector pass and the sync loop.

Glue between collectors, redaction, the hub and digests. The daemon runs this on an interval.
Contract (implemented by the data-layer owner):

    run_sync(config, bots=None, with_sessions=True, commit=True) -> dict
        # {"records": int, "per_bot": {bot: int}, "committed": bool, "at": iso}
    sync_loop(config, interval, bots=None) -> None   # blocking; Ctrl-C to stop

The search index is intentionally *not* rebuilt here: ``ctxbridge sync`` (CLI) owns that cache so
the collector stays cheap and the hub remains the single source of truth.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import replace
from typing import Any

from .config import ConfigError
from .hub import Hub
from .model import utcnow_iso
from .redact import Redactor
from . import digest as digest_module
from .collect import sessions as sessions_collector, superagent as superagent_collector

LOG = logging.getLogger(__name__)


def _selected_config(config: Any, bots: Any) -> Any:
    """Return a config view restricted to ``bots`` (None = every configured bot)."""
    if bots is None:
        return config
    if isinstance(bots, str):
        wanted = [bots]
    else:
        wanted = list(bots)
    unknown = [bot for bot in wanted if bot not in config.bots]
    if unknown:
        raise ConfigError(
            f"unknown bot(s): {', '.join(sorted(unknown))}; configured: {', '.join(sorted(config.bots))}"
        )
    selected = {bot_id: config.bots[bot_id] for bot_id in wanted}
    if not selected:
        raise ConfigError("no bots selected for sync")
    return replace(config, bots=selected)


def _bot_registry(config: Any) -> dict[str, dict[str, Any]]:
    """Display metadata for the hub manifest (no host paths: the manifest is shared data)."""
    registry: dict[str, dict[str, Any]] = {}
    for bot_id, spec in sorted(config.bots.items()):
        registry[bot_id] = {
            "label": spec.label,
            "profile": spec.profile,
            "color": spec.color,
            "emoji": spec.emoji,
        }
    return registry


def run_sync(config: Any, bots: Any = None, with_sessions: bool = True, commit: bool = True) -> dict:
    """Run one incremental collector pass, write the hub, digests, cursors and stats."""
    selected = _selected_config(config, bots)
    hub = Hub(config)
    hub.ensure()
    redactor = Redactor(config)

    cursors = hub.read_cursors()
    if not isinstance(cursors, dict):
        cursors = {}
    records: list[Any] = []

    superagent_records, cursors = superagent_collector.collect(selected, cursors, redactor)
    records.extend(superagent_records)

    session_records: list[Any] = []
    if with_sessions:
        session_records, cursors = sessions_collector.collect(selected, cursors, redactor)
        records.extend(session_records)

    if records:
        hub.write_records(records)

    # Read-modify-write of state/cursors.json under the hub lock, then replace only the sections
    # this run produced so a concurrent board/manifest writer can never lose its update.
    with hub.lock():
        current = hub.read_cursors()
        if not isinstance(current, dict):
            current = {}
        for section in ("superagent", "sessions"):
            if section in cursors:
                current[section] = cursors[section]
        hub.write_cursors(current)

    per_bot: Counter[str] = Counter()
    for record in records:
        per_bot[str(getattr(record, "bot", ""))] += 1
    per_bot_counts = {bot_id: per_bot.get(bot_id, 0) for bot_id in sorted(selected.bots)}
    at = utcnow_iso()

    # The hub merges this into manifest.json (it owns v/schema/updated_at); the registry always
    # covers every configured bot so a filtered `--bot` run cannot shrink it.
    hub.write_manifest(
        {
            "bots": _bot_registry(config),
            "last_sync": {
                "at": at,
                "records": len(records),
                "per_bot": dict(per_bot_counts),
                "with_sessions": bool(with_sessions),
            },
        }
    )
    hub.write_redaction_stats(redactor.stats())

    try:
        digest_module.build_and_write_digests(hub, selected.bots)
    except Exception as exc:  # a broken digest must not abort persistence of memory
        LOG.warning("sync: digest generation failed: %s", exc, exc_info=True)

    committed = False
    if commit:
        message = (
            f"sync: {len(records)} records from {len(per_bot_counts)} bot(s)"
            f" ({sum(1 for n in per_bot_counts.values() if n)} with activity)"
        )
        try:
            committed = bool(hub.commit(message))
        except Exception as exc:  # git problems are reported, not fatal to the collector
            LOG.warning("sync: git commit failed: %s", exc)

    summary = {
        "records": len(records),
        "per_bot": per_bot_counts,
        "committed": committed,
        "at": at,
    }
    LOG.info(
        "sync: %s record(s), committed=%s, per_bot=%s",
        summary["records"],
        summary["committed"],
        ", ".join(f"{bot}:{count}" for bot, count in per_bot_counts.items()),
    )
    return summary


def sync_loop(config: Any, interval: int = 60, bots: Any = None) -> None:
    """Blocking collector daemon: run one pass every ``interval`` seconds until Ctrl-C."""
    interval = max(1, int(interval))
    LOG.info("sync loop started (interval %ss)", interval)
    try:
        while True:
            started = time.monotonic()
            try:
                run_sync(config, bots=bots)
            except Exception as exc:
                LOG.warning("sync: pass failed: %s", exc, exc_info=True)
            elapsed = time.monotonic() - started
            time.sleep(max(1.0, interval - elapsed))
    except KeyboardInterrupt:
        LOG.info("sync loop stopped")


__all__ = ["run_sync", "sync_loop"]
