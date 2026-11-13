"""Orchestration: one collector pass and the sync loop.

Glue between collectors, redaction, the hub and digests. The daemon runs this on an interval.
Contract (implemented by the data-layer owner):

    run_sync(config, bots=None, with_sessions=True, commit=True) -> dict
        # {"records": int, "per_bot": {bot: int}, "committed": bool, "at": iso}
    sync_loop(config, interval, bots=None) -> None   # blocking; Ctrl-C to stop
"""

from __future__ import annotations


def run_sync(config, bots=None, with_sessions: bool = True, commit: bool = True) -> dict:
    raise NotImplementedError("sync.run_sync is implemented by the data-layer owner")


def sync_loop(config, interval: int = 60, bots=None) -> None:
    raise NotImplementedError("sync.sync_loop is implemented by the data-layer owner")
