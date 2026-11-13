"""The hub: a git repository used as the shared memory store.

Layout and concurrency rules are in DESIGN.md §4. This module owns every file access so that
atomicity and locking live in one place. Contract (implemented by the hub/CLI owner):

    hub = Hub(config)
    hub.ensure()                                  # create dirs + git repo
    hub.write_records(records) -> int             # atomic writes, no commit
    hub.commit("message", author=None) -> bool    # git add -A + commit if dirty
    hub.iter_records(bot=None, kind=None)         # generator of Record
    hub.read_record(record_id) -> Record | None
    hub.write_digest(bot, date, markdown)         # digests/<bot>/<date>.md
    hub.read_digest(bot, date) -> str | None
    hub.list_digests(bot) -> list[str]
    hub.read_manifest() / hub.write_manifest(dict)
    hub.read_cursors() / hub.write_cursors(dict)  # state/cursors.json
    hub.write_redaction_stats(dict)               # state/redaction.json
    hub.lock()                                    # contextmanager, fcntl lock
    hub.pull_rebase() / hub.push()
    hub.status() -> dict
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path


class Hub:
    def __init__(self, config, root: str | Path | None = None) -> None:
        self.config = config
        self.root = Path(root or config.hub.dir).expanduser()

    # -- lifecycle -----------------------------------------------------------------
    def ensure(self) -> None:
        raise NotImplementedError("Hub.ensure is implemented by the hub/CLI owner")

    # -- records -------------------------------------------------------------------
    def write_records(self, records) -> int:
        raise NotImplementedError("Hub.write_records is implemented by the hub/CLI owner")

    def write_record(self, record) -> Path:
        raise NotImplementedError("Hub.write_record is implemented by the hub/CLI owner")

    def iter_records(self, bot: str | None = None, kind: str | None = None):
        raise NotImplementedError("Hub.iter_records is implemented by the hub/CLI owner")

    def read_record(self, record_id: str):
        raise NotImplementedError("Hub.read_record is implemented by the hub/CLI owner")

    # -- digests -------------------------------------------------------------------
    def write_digest(self, bot: str, date: str, markdown: str) -> Path:
        raise NotImplementedError("Hub.write_digest is implemented by the hub/CLI owner")

    def read_digest(self, bot: str, date: str) -> str | None:
        raise NotImplementedError("Hub.read_digest is implemented by the hub/CLI owner")

    def list_digests(self, bot: str) -> list[str]:
        raise NotImplementedError("Hub.list_digests is implemented by the hub/CLI owner")

    # -- metadata ------------------------------------------------------------------
    def read_manifest(self) -> dict:
        raise NotImplementedError("Hub.read_manifest is implemented by the hub/CLI owner")

    def write_manifest(self, manifest: dict) -> None:
        raise NotImplementedError("Hub.write_manifest is implemented by the hub/CLI owner")

    def read_cursors(self) -> dict:
        raise NotImplementedError("Hub.read_cursors is implemented by the hub/CLI owner")

    def write_cursors(self, cursors: dict) -> None:
        raise NotImplementedError("Hub.write_cursors is implemented by the hub/CLI owner")

    def write_redaction_stats(self, stats: dict) -> None:
        raise NotImplementedError("Hub.write_redaction_stats is implemented by the hub/CLI owner")

    @contextmanager
    def lock(self):
        """Exclusive lock for read-modify-write objects (board, manifest, cursors)."""
        raise NotImplementedError("Hub.lock is implemented by the hub/CLI owner")
        yield

    # -- git -----------------------------------------------------------------------
    def commit(self, message: str, author: str | None = None) -> bool:
        raise NotImplementedError("Hub.commit is implemented by the hub/CLI owner")

    def pull_rebase(self) -> None:
        raise NotImplementedError("Hub.pull_rebase is implemented by the hub/CLI owner")

    def push(self) -> None:
        raise NotImplementedError("Hub.push is implemented by the hub/CLI owner")

    def status(self) -> dict:
        raise NotImplementedError("Hub.status is implemented by the hub/CLI owner")
