"""The hub: a git repository used as the shared memory store.

Layout and concurrency rules are in DESIGN.md §4. This module owns every file access so that
atomicity and locking live in one place.

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

The module also owns the small number of shared write primitives that :mod:`board` and
:mod:`dispatch` need (``write_json`` / ``write_command`` / ``iter_commands``) so that every
atomic write in the hub still goes through one implementation.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import SCHEMA_VERSION
from .model import BOARD_COLUMNS, Command, Record, normalize_id, record_relpath, utcnow_iso

#: Default git command timeout. The very first `git add -A` of a fresh hub on a network
#: filesystem can take many minutes for tens of thousands of files; allow that, and let the
#: environment override it (``CTXBRIDGE_GIT_TIMEOUT_S``) for slower storage.
GIT_TIMEOUT_S = float(os.environ.get("CTXBRIDGE_GIT_TIMEOUT_S", "900"))
#: A git index lock older than this is debris from a killed git process, safe to remove.
_STALE_LOCK_SECONDS = 300.0
LOCK_TIMEOUT_ENV = "CTXBRIDGE_LOCK_TIMEOUT"
DEFAULT_LOCK_TIMEOUT_S = 30.0

HUB_GITIGNORE = """\
# Hub-local files: deployment config, rebuildable caches and runtime locks.
# The hub is a private data repository; these must never be committed.
config.toml
index/
run/
logs/
*.log
*.tmp
state/lock
"""

_SAFE_AUTHOR = re.compile(r"[^A-Za-z0-9_.-]+")


class HubError(RuntimeError):
    """Raised when a hub operation cannot be completed (bad layout, git failure, ...)."""


def _default_board() -> dict[str, Any]:
    return {
        "columns": [{"id": col_id, "title": title} for col_id, title in BOARD_COLUMNS],
        "cards": [],
        "updated_at": utcnow_iso(),
    }


class Hub:
    def __init__(self, config, root: str | Path | None = None) -> None:
        self.config = config
        if root is not None:
            self.root = Path(root).expanduser()
        elif config is not None and getattr(config, "hub", None) is not None:
            self.root = Path(config.hub.dir).expanduser()
        else:
            raise HubError("a hub root is required when no config is given")
        self._local = threading.local()

    # -- paths ---------------------------------------------------------------------
    @property
    def records_dir(self) -> Path:
        return self.root / "records"

    @property
    def digests_dir(self) -> Path:
        return self.root / "digests"

    @property
    def board_dir(self) -> Path:
        return self.root / "board"

    @property
    def commands_dir(self) -> Path:
        return self.root / "commands"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def index_dir(self) -> Path:
        return self.root / "index"

    @property
    def manifest_file(self) -> Path:
        return self.root / "manifest.json"

    @property
    def board_file(self) -> Path:
        return self.board_dir / "board.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "lock"

    @property
    def cursors_file(self) -> Path:
        return self.state_dir / "cursors.json"

    @property
    def redaction_file(self) -> Path:
        return self.state_dir / "redaction.json"

    def _load_config_commit(self) -> bool:
        return self.config is None or bool(getattr(self.config.hub, "commit", True))

    # -- lifecycle -----------------------------------------------------------------
    def ensure(self) -> None:
        """Create the hub layout and git repository. Idempotent: safe to call on every run."""
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.records_dir,
            self.digests_dir,
            self.board_dir,
            self.commands_dir,
            self.state_dir,
            self.index_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            self.write_text(gitignore, HUB_GITIGNORE)
        self._ensure_git_repo()
        if not self.manifest_file.exists():
            self.write_manifest(self._default_manifest())
        if not self.board_file.exists():
            self.write_json(self.board_file, _default_board())
        if not self.cursors_file.exists():
            self.write_cursors({})
        if not self.redaction_file.exists():
            self.write_redaction_stats({})

    def _default_manifest(self) -> dict[str, Any]:
        now = utcnow_iso()
        bots: dict[str, Any] = {}
        if self.config is not None:
            for bot_id, spec in self.config.bots.items():
                bots[bot_id] = {
                    "id": spec.id,
                    "label": spec.label,
                    "profile": spec.profile,
                    "color": spec.color,
                    "emoji": spec.emoji,
                }
        return {
            "v": SCHEMA_VERSION,
            "schema": SCHEMA_VERSION,
            "created_at": now,
            "updated_at": now,
            "bots": bots,
            "last_sync": None,
        }

    # -- records -------------------------------------------------------------------
    def write_records(self, records: Iterable[Record]) -> int:
        """Atomically write records (no commit). Returns the number of files written."""
        written = 0
        for record in records:
            self.write_record(record)
            written += 1
        return written

    def write_record(self, record: Record) -> Path:
        path = self.root / record_relpath(record)
        self.write_json(path, record.to_dict())
        return path

    def iter_records(self, bot: str | None = None, kind: str | None = None) -> Iterator[Record]:
        """Yield records, optionally filtered by bot and/or kind. Unreadable files are skipped."""
        if not self.records_dir.is_dir():
            return
        if bot:
            bot_dirs = [self.records_dir / normalize_id(bot)]
        else:
            bot_dirs = sorted(p for p in self.records_dir.iterdir() if p.is_dir())
        for bot_dir in bot_dirs:
            if not bot_dir.is_dir():
                continue
            if kind:
                kind_dirs = [bot_dir / normalize_id(kind)]
            else:
                kind_dirs = sorted(p for p in bot_dir.iterdir() if p.is_dir())
            for kind_dir in kind_dirs:
                if not kind_dir.is_dir():
                    continue
                for path in sorted(kind_dir.glob("*.json")):
                    data = self._read_json(path, None)
                    if not isinstance(data, dict):
                        continue
                    try:
                        yield Record.from_dict(data)
                    except (TypeError, ValueError, KeyError):
                        continue

    def read_record(self, record_id: str) -> Record | None:
        """Read one record by its stable ``<bot>:<kind>:<source-id>`` id."""
        if not record_id or record_id.count(":") < 2:
            return None
        bot, kind, _source = record_id.split(":", 2)
        path = (
            self.records_dir
            / normalize_id(bot)
            / normalize_id(kind)
            / f"{normalize_id(record_id)}.json"
        )
        data = self._read_json(path, None)
        if not isinstance(data, dict):
            return None
        try:
            return Record.from_dict(data)
        except (TypeError, ValueError, KeyError):
            return None

    # -- digests -------------------------------------------------------------------
    def write_digest(self, bot: str, date: str, markdown: str) -> Path:
        path = self.digests_dir / normalize_id(bot) / f"{normalize_id(date)}.md"
        self.write_text(path, markdown if markdown.endswith("\n") else markdown + "\n")
        return path

    def read_digest(self, bot: str, date: str) -> str | None:
        path = self.digests_dir / normalize_id(bot) / f"{normalize_id(date)}.md"
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def list_digests(self, bot: str) -> list[str]:
        """Digest dates for one bot, newest first."""
        directory = self.digests_dir / normalize_id(bot)
        if not directory.is_dir():
            return []
        return sorted((p.stem for p in directory.glob("*.md")), reverse=True)

    # -- metadata ------------------------------------------------------------------
    def read_manifest(self) -> dict[str, Any]:
        data = self._read_json(self.manifest_file, {})
        return data if isinstance(data, dict) else {}

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        """Merge ``manifest`` into the stored manifest and atomically replace it.

        When the update carries ``last_sync`` information it is mirrored into
        ``state/last-sync.json`` (part of the documented hub layout) so simple readers and the
        API health check do not need to understand the whole manifest.
        """
        with self.lock():
            current = self.read_manifest()
            merged: dict[str, Any] = {**current, **(manifest or {})}
            merged["v"] = SCHEMA_VERSION
            merged["updated_at"] = utcnow_iso()
            merged.setdefault("schema", SCHEMA_VERSION)
            self.write_json(self.manifest_file, merged)
            last_sync = merged.get("last_sync")
            if isinstance(last_sync, str) and last_sync:
                last_sync = {"at": last_sync}
            if isinstance(last_sync, dict) and last_sync:
                self.write_json(self.state_dir / "last-sync.json", dict(last_sync))

    def read_cursors(self) -> dict[str, Any]:
        data = self._read_json(self.cursors_file, {})
        return data if isinstance(data, dict) else {}

    def write_cursors(self, cursors: dict[str, Any]) -> None:
        with self.lock():
            self.write_json(self.cursors_file, cursors or {})

    def read_redaction_stats(self) -> dict[str, Any]:
        data = self._read_json(self.redaction_file, {})
        return data if isinstance(data, dict) else {}

    def write_redaction_stats(self, stats: dict[str, Any]) -> None:
        with self.lock():
            self.write_json(self.redaction_file, stats or {})

    # -- commands (relayed instructions) ------------------------------------------
    def write_command(self, command: Command) -> Path:
        path = self.commands_dir / f"{normalize_id(command.id)}.json"
        self.write_json(path, command.to_dict())
        return path

    def read_command(self, command_id: str) -> Command | None:
        path = self.commands_dir / f"{normalize_id(command_id)}.json"
        data = self._read_json(path, None)
        if not isinstance(data, dict):
            return None
        try:
            return Command(
                target_bot=str(data.get("target_bot", "")),
                text=str(data.get("text", "")),
                id=str(data.get("id", "")),
                via=str(data.get("via", "feishu")),
                status=str(data.get("status", "pending")),
                message_id=data.get("message_id"),
                error=data.get("error"),
                created_at=str(data.get("created_at", "")),
            )
        except (TypeError, ValueError):
            return None

    def iter_commands(self, limit: int | None = None) -> list[Command]:
        """Recorded commands, newest first."""
        if not self.commands_dir.is_dir():
            return []
        commands: list[Command] = []
        for path in self.commands_dir.glob("*.json"):
            data = self._read_json(path, None)
            if not isinstance(data, dict):
                continue
            try:
                commands.append(
                    Command(
                        target_bot=str(data.get("target_bot", "")),
                        text=str(data.get("text", "")),
                        id=str(data.get("id", "")),
                        via=str(data.get("via", "feishu")),
                        status=str(data.get("status", "pending")),
                        message_id=data.get("message_id"),
                        error=data.get("error"),
                        created_at=str(data.get("created_at", "")),
                    )
                )
            except (TypeError, ValueError):
                continue
        commands.sort(key=lambda c: c.created_at, reverse=True)
        return commands[:limit] if limit is not None else commands

    # -- atomic file primitives ----------------------------------------------------
    def write_json(self, path: str | Path, data: Any) -> Path:
        """Atomically write JSON: temp file in the target directory, fsync, ``os.replace``."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return target

    def write_text(self, path: str | Path, text: str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return target

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return default

    # -- locking -------------------------------------------------------------------
    @contextmanager
    def lock(self, timeout: float | None = None):
        """Exclusive, re-entrant (per thread) lock for read-modify-write objects."""
        if timeout is None:
            try:
                timeout = float(os.environ.get(LOCK_TIMEOUT_ENV, DEFAULT_LOCK_TIMEOUT_S))
            except ValueError:
                timeout = DEFAULT_LOCK_TIMEOUT_S

        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
            return

        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o644)
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"timed out after {timeout:.1f}s waiting for hub lock {self.lock_file}"
                        ) from None
                    time.sleep(0.02)
            self._local.depth = 1
            try:
                yield
            finally:
                self._local.depth = 0
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # -- git -----------------------------------------------------------------------
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_S,
            )
        except FileNotFoundError:
            raise HubError("git is required but was not found on PATH") from None
        except subprocess.TimeoutExpired:
            raise HubError(f"git {args[0] if args else ''} timed out") from None
        if check and proc.returncode != 0:
            detail = " ".join((proc.stderr or proc.stdout or "").strip().split())[:400]
            raise HubError(f"git {' '.join(args)} failed: {detail or 'unknown error'}")
        return proc

    def _ensure_git_repo(self) -> None:
        if (self.root / ".git").is_dir():
            self._ensure_remote()
            return
        try:
            proc = subprocess.run(
                ["git", "init", "-q", "-b", "main"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_S,
            )
        except FileNotFoundError:
            raise HubError("git is required to create a hub; install git and retry") from None
        if proc.returncode != 0:
            # Older git without `-b`; fall back to init + symbolic-ref.
            self._git("init", "-q")
            try:
                self._git("symbolic-ref", "HEAD", "refs/heads/main")
            except HubError:
                pass
        self._ensure_remote()

    def _ensure_remote(self) -> None:
        remote = ""
        if self.config is not None:
            remote = str(getattr(self.config.hub, "remote", "") or "").strip()
        if not remote:
            return
        existing = self._git("remote", check=False)
        if existing.returncode != 0:
            return
        names = existing.stdout.split()
        if "origin" not in names and remote not in names:
            self._git("remote", "add", "origin", remote)

    def _has_remote(self) -> bool:
        if not (self.root / ".git").is_dir():
            return False
        remote = ""
        if self.config is not None:
            remote = str(getattr(self.config.hub, "remote", "") or "").strip()
        if remote:
            return True
        names = self._git("remote", check=False)
        return names.returncode == 0 and bool(names.stdout.strip())

    def commit(self, message: str, author: str | None = None) -> bool:
        """Stage everything and commit if dirty. Returns True when a commit was created.

        ``git add`` and ``git commit`` are retried a few times: a long initial commit on a
        network filesystem can outlive the timeout and leave ``.git/index.lock`` behind, and
        the 60-second sync loop would otherwise fail every later pass. A lock older than
        ``_STALE_LOCK_SECONDS`` is treated as debris from a killed git process.
        """
        if not self._load_config_commit():
            return False
        if not (self.root / ".git").is_dir():
            return False
        staged = None
        for attempt in range(3):
            try:
                self._git("add", "-A")
                staged = self._git("diff", "--cached", "--quiet", check=False)
                break
            except HubError as exc:
                if attempt == 2 or "index.lock" not in str(exc):
                    raise
                self._clear_stale_lock()
                time.sleep(1.5)
        if staged is None:
            raise HubError("git add failed")
        if staged.returncode == 0:
            return False
        if staged.returncode != 1:
            detail = " ".join((staged.stderr or staged.stdout or "").strip().split())[:400]
            raise HubError(f"git diff --cached failed: {detail or 'unknown error'}")
        name = (author or "ctxbridge").strip() or "ctxbridge"
        email = f"{_SAFE_AUTHOR.sub('-', name) or 'ctxbridge'}@localhost"
        for attempt in range(3):
            try:
                self._git(
                    "-c",
                    f"user.name={name}",
                    "-c",
                    f"user.email={email}",
                    "commit",
                    "-q",
                    "-m",
                    message or "hub update",
                )
                return True
            except HubError as exc:
                if attempt == 2 or "index.lock" not in str(exc):
                    raise
                self._clear_stale_lock()
                time.sleep(1.5)
        return False

    def _clear_stale_lock(self) -> None:
        """Remove ``.git/index.lock`` only when it is old enough to be debris."""
        lock = self.root / ".git" / "index.lock"
        try:
            if lock.is_file() and time.time() - lock.stat().st_mtime > _STALE_LOCK_SECONDS:
                lock.unlink()
        except OSError:
            return

    def pull_rebase(self) -> None:
        """``git pull --rebase`` when a remote is configured; silently a no-op otherwise."""
        if not self._has_remote():
            return
        self._git("pull", "--rebase", "--autostash", "-q")

    def push(self) -> None:
        """Push the current branch to ``origin``; silently a no-op without a remote or commits."""
        if not self._has_remote() or self._last_commit() is None:
            return
        self._git("push", "-q", "origin", "HEAD")

    def _last_commit(self) -> dict[str, str] | None:
        if not (self.root / ".git").is_dir():
            return None
        proc = self._git("log", "-1", "--format=%H%x00%cI%x00%s", check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        parts = proc.stdout.strip().split("\x00")
        if len(parts) < 3:
            return None
        return {"hash": parts[0], "at": parts[1], "subject": parts[2]}

    # -- status --------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Counts by bot/kind plus last sync/commit information; used by ``ctxbridge status``."""
        by_bot: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        records = 0
        if self.records_dir.is_dir():
            for path in sorted(self.records_dir.rglob("*.json")):
                parts = path.relative_to(self.records_dir).parts
                if len(parts) < 3:
                    continue
                records += 1
                by_bot[parts[0]] = by_bot.get(parts[0], 0) + 1
                by_kind[parts[1]] = by_kind.get(parts[1], 0) + 1
        board_cards = 0
        board = self._read_json(self.board_file, {})
        if isinstance(board, dict) and isinstance(board.get("cards"), list):
            board_cards = len(board["cards"])
        commands = len(list(self.commands_dir.glob("*.json"))) if self.commands_dir.is_dir() else 0
        digests = len(list(self.digests_dir.rglob("*.md"))) if self.digests_dir.is_dir() else 0
        manifest = self.read_manifest()
        dirty: bool | None = None
        if (self.root / ".git").is_dir():
            proc = self._git("status", "--porcelain", check=False)
            if proc.returncode == 0:
                dirty = bool(proc.stdout.strip())
        return {
            "dir": str(self.root),
            "initialized": self.manifest_file.exists(),
            "records": records,
            "by_bot": by_bot,
            "by_kind": by_kind,
            "bots": sorted(by_bot),
            "board_cards": board_cards,
            "commands": commands,
            "digests": digests,
            "last_sync": self._last_sync(manifest),
            "last_commit": self._last_commit(),
            "dirty": dirty,
        }

    def _last_sync(self, manifest: dict[str, Any]) -> str | None:
        value = manifest.get("last_sync")
        if isinstance(value, dict):
            at = value.get("at") or value.get("time")
            if at:
                return str(at)
        elif isinstance(value, str) and value:
            return value
        for key in ("last_sync_at", "synced_at"):
            if manifest.get(key):
                return str(manifest[key])
        state_file = self._read_json(self.state_dir / "last-sync.json", {})
        if isinstance(state_file, dict) and state_file.get("at"):
            return str(state_file["at"])
        return None
