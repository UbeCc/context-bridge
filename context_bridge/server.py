"""HTTP server: JSON API + static web console + SSE change feed.

Standard library only (``http.server.ThreadingHTTPServer``). The wire contract is ``API.md``;
the console lives in ``web/``. Public entry points::

    server = create_server(config, hub=None, web_dir=None)   # bound, not started
    server = run_in_thread(config, hub=None, web_dir=None)   # started in a daemon thread
    serve(config)                                            # blocking serve_forever

Implementation notes (interpretations the API contract leaves open):

- ``/api/health`` reads ``state/redaction.json`` and ``state/last-sync.json`` from the hub
  directly: the Hub API exposes writers but no readers for those two files. Missing files are
  reported as zero / ``null``.
- "attention" is derived from open ``ticket`` records: a non-empty ``payload.attention`` (string,
  dict or ``True``) or a latest report whose state is one of ``attention/blocked/question/waiting``.
  The console's ``question`` field is that attention text, else the latest report summary, else the
  ticket title. Tickets whose payload state is ``ended/done/closed/cancelled/ignored`` are ignored.
- A bot's ``status`` is based on its newest record: ``active`` <= 24h, ``idle`` <= 7 days,
  ``stale`` beyond that, ``unknown`` when the bot has no records yet.
- With ``server.token`` set, the token is required for ``/api/*`` and for HTML documents. It is
  accepted as ``Authorization: Bearer <token>``, as ``?token=<token>`` (so a browser navigation
  works without custom headers) or as the ``ctxbridge_token`` cookie that a successful
  ``?token=`` request sets. Static assets (JS/CSS/fonts/images) are served without the token so
  a page can still boot; API data stays protected.

Concurrency: request threads are daemons; the SSE feed is fed by one hub-watching poller thread.
``POST /api/sync`` is single-flight and never blocks a request thread.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import __version__
from . import dispatch as dispatch_mod
from .board import Board
from .hub import Hub
from .index import SearchIndex
from .model import BOARD_COLUMNS, BOARD_COLUMN_IDS, Record, utcnow_iso

logger = logging.getLogger("context_bridge.server")

MAX_BODY = 1 << 20  # 1 MiB, generous for console payloads
SSE_HEARTBEAT_S = 15.0
WATCH_INTERVAL_S = 1.0
ANALYSIS_TTL_S = 2.0
STATUS_ACTIVE_S = 24 * 3600
STATUS_IDLE_S = 7 * 24 * 3600

_TOKEN_COOKIE = "ctxbridge_token"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".wasm": "application/wasm",
    ".xml": "application/xml",
    ".pdf": "application/pdf",
}
_TEXT_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".map", ".svg", ".txt", ".md", ".webmanifest", ".xml"}

_PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Context Bridge</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;margin:3rem;line-height:1.6">
<h1>Context Bridge</h1>
<p>The web console is not installed yet: <code>web/index.html</code> is missing.</p>
<p>The JSON API is available at <a href="/api/health">/api/health</a>.</p>
</body></html>"""


# ---------------------------------------------------------------------------------------------
# errors / small helpers
# ---------------------------------------------------------------------------------------------
class _HttpError(Exception):
    """Raised inside a handler to produce an API error response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _first(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name) or []
    return values[0] if values else None


def _text_arg(query: dict[str, list[str]], name: str) -> str:
    value = _first(query, name)
    return value.strip() if value else ""


def _bot_filter(query: dict[str, list[str]]) -> str | None:
    value = _text_arg(query, "bot")
    if value in ("", "all", "*"):
        return None
    return value


def _int_arg(query: dict[str, list[str]], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _first(query, name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _HttpError(400, "bad_request", f"invalid integer for {name!r}: {raw!r}") from None
    return max(minimum, min(maximum, value))


def _json_file(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read a small JSON object file, returning ``default`` when it is missing/corrupt."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(default or {})
    return data if isinstance(data, dict) else dict(default or {})


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(value: Any) -> float | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds()


# ---------------------------------------------------------------------------------------------
# hub-adjacent helpers
# ---------------------------------------------------------------------------------------------
def _manifest(hub: Hub) -> dict[str, Any]:
    try:
        data = hub.read_manifest()
    except NotImplementedError:
        return {}
    except Exception:  # pragma: no cover - defensive: a corrupt manifest must not kill the API
        logger.exception("failed to read manifest")
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_last_sync(hub: Hub, manifest: dict[str, Any] | None = None) -> str | None:
    data = manifest if manifest is not None else _manifest(hub)
    raw = data.get("last_sync")
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        for key in ("at", "time", "timestamp"):
            if isinstance(raw.get(key), str) and raw[key]:
                return raw[key]
    last = _json_file(Path(hub.root) / "state" / "last-sync.json")
    for key in ("at", "time", "timestamp", "last_sync"):
        if isinstance(last.get(key), str) and last[key]:
            return last[key]
    return None


def _bot_last_sync(hub: Hub, bot_id: str, manifest: dict[str, Any]) -> str | None:
    bots = manifest.get("bots")
    entry: Any = None
    if isinstance(bots, dict):
        entry = bots.get(bot_id)
    elif isinstance(bots, list):
        entry = next((b for b in bots if isinstance(b, dict) and b.get("id") == bot_id), None)
    if isinstance(entry, dict):
        raw = entry.get("last_sync") or entry.get("synced_at")
        if isinstance(raw, str) and raw:
            return raw
        if isinstance(raw, dict):
            for key in ("at", "time"):
                if isinstance(raw.get(key), str) and raw[key]:
                    return raw[key]
    return None


def _redaction_stats(hub: Hub) -> dict[str, Any]:
    reader = getattr(hub, "read_redaction_stats", None)
    if callable(reader):
        try:
            data = reader()
        except Exception:  # pragma: no cover - defensive: stats are informational
            logger.exception("failed to read redaction stats")
            data = {}
    else:  # pragma: no cover - alternative Hub implementations without the reader
        data = _json_file(Path(hub.root) / "state" / "redaction.json")
    if not isinstance(data, dict):
        data = {}
    total = data.get("total", data.get("count", data.get("redactions", 0)))
    try:
        total_int = int(total)
    except (TypeError, ValueError):
        total_int = 0
    by_rule = data.get("by_rule", data.get("rules", data.get("by-rule")))
    if not isinstance(by_rule, dict):
        by_rule = {}
    return {"total": total_int, "by_rule": {str(k): v for k, v in by_rule.items()}}


def _iter_records(hub: Hub, bot: str | None = None, kind: str | None = None):
    """Iterate hub records; the Hub contract returns a generator of :class:`Record`."""
    return hub.iter_records(bot=bot, kind=kind)


def _matches_q(record: Record, query: str) -> bool:
    needle = query.casefold()
    haystack = " ".join(
        [record.title or "", record.summary or "", record.bot or "", record.kind or "", " ".join(record.tags or [])]
    ).casefold()
    return needle in haystack


def _record_sort_key(record: Record) -> tuple[str, str]:
    return (record.updated_at or record.created_at or "", record.id)


def _ticket_open(record: Record) -> bool:
    if record.kind != "ticket":
        return False
    payload = record.payload or {}
    state = payload.get("state")
    if isinstance(state, str) and state:
        return state.lower() not in ("ended", "done", "closed", "cancelled", "ignored", "stopped")
    if "ended" in payload:
        return not bool(payload.get("ended"))
    if "open" in payload:
        return bool(payload.get("open"))
    return True


def _attention_question(record: Record) -> str | None:
    """Return the human-facing question for an attention-needing open ticket, if any."""
    if not _ticket_open(record):
        return None
    payload = record.payload or {}
    question: Any = None
    attention = payload.get("attention")
    if isinstance(attention, dict):
        if attention.get("active") is False or attention.get("handled") is True:
            return None
        question = attention.get("question") or attention.get("summary") or attention.get("text")
    elif isinstance(attention, str):
        question = attention.strip() or None
    elif attention is True:
        question = None
        if isinstance(payload.get("question"), str) and payload["question"].strip():
            question = payload["question"]

    reports = payload.get("reports")
    latest: dict[str, Any] = {}
    if isinstance(reports, list) and reports:
        last = reports[-1]
        if isinstance(last, dict):
            latest = last
    if question is None:
        report_state = str(latest.get("state", "")).lower()
        if report_state in ("attention", "blocked", "question", "waiting", "needs_input"):
            question = latest.get("summary") or record.title
        elif isinstance(attention, str) or attention is True:
            question = latest.get("summary") or record.title
    if question is None:
        return None
    return str(question).strip()[:600]


def _bot_status(latest_record_at: str | None) -> str:
    age = _age_seconds(latest_record_at)
    if age is None:
        return "unknown"
    if age <= STATUS_ACTIVE_S:
        return "active"
    if age <= STATUS_IDLE_S:
        return "idle"
    return "stale"


def _hub_fingerprint(hub: Hub) -> dict[str, Any]:
    """Cheap change detector for the hub.

    Some filesystems expose only whole-second ``st_mtime`` values, so mtime alone misses changes
    made within the same second as the previous poll. Records are therefore
    fingerprinted by (file count, total size, newest mtime) per bot/kind directory, while the small
    metadata files and the board get a content hash. That keeps the one-second polling loop cheap
    without relying on timestamp resolution.
    """
    root = Path(hub.root)
    records_parts: list[tuple[str, str, int, int, int]] = []
    total = 0
    records_dir = root / "records"
    if records_dir.is_dir():
        try:
            with os.scandir(records_dir) as bots:
                for bot_entry in bots:
                    if not bot_entry.is_dir():
                        continue
                    try:
                        with os.scandir(bot_entry.path) as kinds:
                            for kind_entry in kinds:
                                if not kind_entry.is_dir():
                                    continue
                                count = 0
                                total_size = 0
                                newest = 0
                                try:
                                    with os.scandir(kind_entry.path) as files:
                                        for file_entry in files:
                                            if not file_entry.name.endswith(".json"):
                                                continue
                                            try:
                                                stat = file_entry.stat()
                                            except OSError:
                                                continue
                                            count += 1
                                            total_size += stat.st_size
                                            newest = max(newest, stat.st_mtime_ns)
                                except OSError:
                                    continue
                                total += count
                                records_parts.append(
                                    (bot_entry.name, kind_entry.name, count, total_size, newest)
                                )
                    except OSError:
                        continue
        except OSError:
            pass
    records_parts.sort()

    meta = tuple(
        _content_signature(root / rel)
        for rel in ("manifest.json", "state/last-sync.json", "state/redaction.json")
    )
    board = _content_signature(root / "board" / "board.json")
    commands = _command_signature(root / "commands")
    return {
        "records": tuple(records_parts),
        "records_total": total,
        "meta": meta,
        "board": board,
        "commands": commands,
    }


def _content_signature(path: Path) -> tuple[Any, ...]:
    """(size, mtime_ns, sha1) for a small JSON file; mtime alone is not precise enough here."""
    try:
        stat = path.stat()
        digest = hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return (None, None, None)
    return (stat.st_size, stat.st_mtime_ns, digest)


def _command_signature(directory: Path) -> tuple[Any, ...]:
    entries: list[tuple[str, int, int]] = []
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                entries.append((entry.name, stat.st_size, stat.st_mtime_ns))
    except OSError:
        return ()
    entries.sort()
    return tuple(entries)


def _newest_command_id(hub: Hub) -> str | None:
    commands_dir = Path(hub.root) / "commands"
    entries: list[tuple[int, str, Path]] = []
    try:
        with os.scandir(commands_dir) as scan:
            for entry in scan:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                entries.append((stat.st_mtime_ns, entry.name, Path(entry.path)))
    except OSError:
        return None
    if not entries:
        return None
    entries.sort(reverse=True)
    data = _json_file(entries[0][2])
    if isinstance(data.get("id"), str) and data["id"]:
        return data["id"]
    return Path(entries[0][1]).stem or None


def _build_analysis(hub: Hub, config) -> dict[str, Any]:
    """Aggregate every record once: per-bot counters, activity, recent items, attention list."""
    per_bot: dict[str, dict[str, Any]] = {
        bot_id: {"counts": {}, "open_tickets": 0, "attention": 0, "latest": None}
        for bot_id in getattr(config, "bots", {})
    }
    by_kind: dict[str, int] = {}
    attention_items: list[dict[str, Any]] = []
    recent: list[Record] = []
    activity: dict[str, int] = {}
    today = datetime.now(timezone.utc).date()
    for offset in range(13, -1, -1):
        activity[(today - timedelta(days=offset)).isoformat()] = 0

    for record in _iter_records(hub):
        bot = record.bot or "unknown"
        entry = per_bot.setdefault(bot, {"counts": {}, "open_tickets": 0, "attention": 0, "latest": None})
        kind = record.kind or "unknown"
        entry["counts"][kind] = entry["counts"].get(kind, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
        stamp = record.updated_at or record.created_at or ""
        if stamp and (entry["latest"] is None or stamp > entry["latest"]):
            entry["latest"] = stamp
        if _ticket_open(record):
            entry["open_tickets"] += 1
        question = _attention_question(record)
        if question is not None:
            entry["attention"] += 1
            attention_items.append(
                {
                    "bot": record.bot,
                    "record_id": record.id,
                    "title": record.title,
                    "question": question,
                    "at": record.updated_at or record.created_at,
                }
            )
        day = (record.created_at or record.updated_at or "")[:10]
        if day in activity:
            activity[day] += 1
        recent.append(record)

    recent.sort(key=_record_sort_key, reverse=True)
    attention_items.sort(key=lambda item: item.get("at") or "", reverse=True)
    manifest = _manifest(hub)
    global_last_sync = _manifest_last_sync(hub, manifest)
    bot_last_sync = {
        bot_id: _bot_last_sync(hub, bot_id, manifest) or global_last_sync for bot_id in per_bot
    }
    return {
        "per_bot": per_bot,
        "by_kind": by_kind,
        "records_total": sum(by_kind.values()),
        "activity": [{"date": day, "count": count} for day, count in activity.items()],
        "recent": [record.brief() for record in recent[:12]],
        "attention": attention_items,
        "last_sync": global_last_sync,
        "bot_last_sync": bot_last_sync,
    }


# ---------------------------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------------------------
class ContextBridgeServer(ThreadingHTTPServer):
    """Threading HTTP server carrying the config/hub and the SSE fan-out state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_cls, config, hub: Hub, web_dir: Path) -> None:
        super().__init__(address, handler_cls)
        self.config = config
        self.hub = hub
        self.web_dir = Path(web_dir)
        self.token = str(getattr(config.server, "token", "") or "")
        self.started_monotonic = time.monotonic()
        self._sse_clients: list[queue.Queue] = []
        self._sse_lock = threading.Lock()
        self._stop = threading.Event()
        self._sync_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._analysis_cache: tuple[Any, float, dict[str, Any]] | None = None
        # The baseline must be captured *now*, not from inside the watcher thread: the new thread
        # may only be scheduled after the caller has already written to the hub, which would make
        # those changes invisible forever. Taking it synchronously guarantees that every change
        # made after create_server() returns is detected and broadcast.
        self._watch_baseline = _hub_fingerprint(self.hub)
        self._watcher = threading.Thread(
            target=self._watch_loop, args=(self._watch_baseline,), name="ctxbridge-watcher", daemon=True
        )
        self._watcher.start()

    # -- lifecycle -----------------------------------------------------------------
    def server_close(self) -> None:
        self._stop.set()
        with self._sse_lock:
            clients = list(self._sse_clients)
            self._sse_clients.clear()
        for client in clients:
            client.put(None)
        super().server_close()

    # -- SSE fan-out ---------------------------------------------------------------
    def register_sse(self, client: queue.Queue) -> None:
        with self._sse_lock:
            self._sse_clients.append(client)

    def unregister_sse(self, client: queue.Queue) -> None:
        with self._sse_lock:
            try:
                self._sse_clients.remove(client)
            except ValueError:
                pass

    def broadcast(self, event: str, data: dict[str, Any]) -> None:
        with self._sse_lock:
            clients = list(self._sse_clients)
        for client in clients:
            try:
                client.put_nowait((event, data))
            except queue.Full:  # pragma: no cover - unbounded queues
                pass

    # -- hub watching ---------------------------------------------------------------
    def _watch_loop(self, previous: dict[str, Any]) -> None:
        while not self._stop.wait(WATCH_INTERVAL_S):
            try:
                current = _hub_fingerprint(self.hub)
            except Exception:  # pragma: no cover - defensive polling
                logger.exception("hub fingerprint failed")
                continue
            try:
                if previous["records"] != current["records"] or previous["meta"] != current["meta"]:
                    self.broadcast(
                        "sync",
                        {"at": utcnow_iso(), "records": current["records_total"]},
                    )
                if previous["board"] != current["board"]:
                    self.broadcast("board", {"at": utcnow_iso()})
                if previous["commands"] != current["commands"]:
                    self.broadcast("command", {"id": _newest_command_id(self.hub)})
            finally:
                previous = current

    # -- analysis cache -------------------------------------------------------------
    def analysis(self) -> dict[str, Any]:
        fingerprint = _hub_fingerprint(self.hub)
        signature = (fingerprint["records"], fingerprint["records_total"])
        now = time.monotonic()
        with self._cache_lock:
            cached = self._analysis_cache
            if cached and cached[0] == signature and (now - cached[1]) <= ANALYSIS_TTL_S:
                return cached[2]
        data = _build_analysis(self.hub, self.config)
        with self._cache_lock:
            self._analysis_cache = (signature, time.monotonic(), data)
        return data

    def invalidate_analysis(self) -> None:
        with self._cache_lock:
            self._analysis_cache = None

    # -- background sync ------------------------------------------------------------
    def start_sync(self) -> bool:
        """Start one collector pass in the background. Returns False if already running."""
        if not self._sync_lock.acquire(blocking=False):
            return False
        thread = threading.Thread(target=self._run_sync, name="ctxbridge-sync", daemon=True)
        try:
            thread.start()
        except Exception:  # pragma: no cover - thread start is effectively infallible
            self._sync_lock.release()
            raise
        return True

    def _run_sync(self) -> None:
        try:
            from .sync import run_sync

            result = run_sync(self.config, commit=True)
            records = None
            at = None
            if isinstance(result, dict):
                records = result.get("records")
                at = result.get("at")
            if not isinstance(records, int):
                records = _hub_fingerprint(self.hub)["records_total"]
            self.invalidate_analysis()
            self.broadcast("sync", {"at": at or utcnow_iso(), "records": records})
        except Exception:  # pragma: no cover - background failures are logged, not raised
            logger.exception("background sync failed")
        finally:
            self._sync_lock.release()

    def bot_summary(self, bot_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
        spec = self.config.bots[bot_id]
        entry = analysis["per_bot"].get(bot_id) or {
            "counts": {},
            "open_tickets": 0,
            "attention": 0,
            "latest": None,
        }
        return {
            "id": spec.id,
            "label": spec.label,
            "profile": spec.profile,
            "color": spec.color,
            "emoji": spec.emoji,
            "status": _bot_status(entry.get("latest")),
            "last_sync": analysis["bot_last_sync"].get(bot_id),
            "counts": dict(sorted(entry.get("counts", {}).items())),
            "open_tickets": int(entry.get("open_tickets", 0)),
            "attention": int(entry.get("attention", 0)),
        }


# ---------------------------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = f"ContextBridge/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- low level ------------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib API name
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send_headers(self, status: int, content_type: str, length: int | None, extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self, "_auth_cookie", False):
            token = self.server.token  # type: ignore[attr-defined]
            if token:
                self.send_header(
                    "Set-Cookie",
                    f"{_TOKEN_COOKIE}={quote(token, safe='')}; Path=/; HttpOnly; SameSite=Strict",
                )
        for name, value in extra or ():
            self.send_header(name, value)
        self.end_headers()
        self._headers_sent = True

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_headers(status, "application/json; charset=utf-8", len(body))
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_bytes(self, status: int, body: bytes, content_type: str, extra: list[tuple[str, str]] | None = None) -> None:
        self._send_headers(status, content_type, len(body), extra)
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_error(self, status: int, code: str, message: str, extra: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"error": {"code": code, "message": message}}
        if extra:
            for key, value in extra.items():
                if key != "error":
                    payload[key] = value
        self._send_json(status, payload)

    def _send_http_error(self, error: _HttpError, extra: dict[str, Any] | None = None) -> None:
        self._send_error(error.status, error.code, error.message, extra)

    # -- auth -----------------------------------------------------------------------
    def _authorized(self, query: dict[str, list[str]]) -> bool:
        token = self.server.token  # type: ignore[attr-defined]
        if not token:
            return True
        header = self.headers.get("Authorization") or ""
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value and hmac.compare_digest(value.strip(), token):
            return True
        supplied = _first(query, "token")
        if supplied and hmac.compare_digest(supplied, token):
            self._auth_cookie = True
            return True
        cookie = self.headers.get("Cookie") or ""
        for chunk in cookie.split(";"):
            name, _, value = chunk.strip().partition("=")
            if name == _TOKEN_COOKIE and value:
                if hmac.compare_digest(unquote(value), token):
                    return True
        return False

    def _require_auth(self, query: dict[str, list[str]], html: bool = False) -> None:
        if not self._authorized(query):
            extra = [("WWW-Authenticate", "Bearer")]
            self.send_response(401)
            body = json.dumps(
                {"error": {"code": "unauthorized", "message": "missing or invalid bearer token"}}
            ).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            self._headers_sent = True
            self.wfile.write(body)
            raise _Handled()

    # -- request body ---------------------------------------------------------------
    def _read_json_body(self, required: bool = True) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or raw_length == "":
            if required:
                raise _HttpError(400, "bad_request", "request body is required")
            return {}
        try:
            length = int(raw_length)
        except ValueError:
            raise _HttpError(400, "bad_request", "invalid Content-Length") from None
        if length < 0 or length > MAX_BODY:
            raise _HttpError(413, "payload_too_large", f"request body must be <= {MAX_BODY} bytes")
        body = self.rfile.read(length)
        if not body.strip():
            return {}
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise _HttpError(400, "invalid_json", f"invalid JSON body: {exc}") from None
        if not isinstance(data, dict):
            raise _HttpError(400, "invalid_json", "request body must be a JSON object")
        return data

    # -- entry points ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib API name
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        self._headers_sent = False
        self._auth_cookie = False
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query, keep_blank_values=True)
        try:
            if method == "GET" and path == "/api/events":
                self._require_auth(query)
                self._serve_sse()
                return
            if path.startswith("/api/"):
                self._require_auth(query)
                self._route_api(method, path, query)
                return
            if path in ("/api", "/api/"):
                raise _HttpError(404, "not_found", f"unknown endpoint: {path}")
            if method == "GET":
                self._serve_static(path, query)
                return
            raise _HttpError(405, "method_not_allowed", f"{method} is not allowed for {path}")
        except _Handled:
            return
        except _HttpError as error:
            if not self._headers_sent:
                self._send_http_error(error)
            else:  # pragma: no cover - headers already on the wire
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # pragma: no cover - unexpected: log traceback, return generic 500
            logger.exception("unhandled error for %s %s", method, self.path)
            if not self._headers_sent:
                self._send_error(500, "internal", "internal server error")
            else:
                self.close_connection = True

    # -- API routing ----------------------------------------------------------------
    def _route_api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        if method == "GET":
            if path == "/api/health":
                return self._api_health()
            if path == "/api/overview":
                return self._api_overview()
            if path == "/api/bots":
                return self._api_bots()
            if path.startswith("/api/bots/"):
                return self._api_bot_detail(unquote(path[len("/api/bots/"):]))
            if path.startswith("/api/digests/"):
                return self._api_digest(unquote(path[len("/api/digests/"):]))
            if path == "/api/records":
                return self._api_records(query)
            if path.startswith("/api/records/"):
                return self._api_record_detail(unquote(path[len("/api/records/"):]))
            if path == "/api/search":
                return self._api_search(query)
            if path == "/api/board":
                return self._api_board()
            if path == "/api/commands":
                return self._api_commands(query)
            raise _HttpError(404, "not_found", f"unknown endpoint: {path}")
        if method == "POST":
            if path == "/api/board/cards":
                return self._api_board_create()
            if path == "/api/commands":
                return self._api_command_create()
            if path == "/api/sync":
                return self._api_sync()
            raise _HttpError(404, "not_found", f"unknown endpoint: {path}")
        if method == "PATCH":
            if path.startswith("/api/board/cards/"):
                return self._api_board_patch(unquote(path[len("/api/board/cards/"):]))
            raise _HttpError(404, "not_found", f"unknown endpoint: {path}")
        if method == "DELETE":
            if path.startswith("/api/board/cards/"):
                return self._api_board_delete(unquote(path[len("/api/board/cards/"):]))
            raise _HttpError(404, "not_found", f"unknown endpoint: {path}")
        raise _HttpError(405, "method_not_allowed", f"{method} is not allowed for {path}")

    # -- read endpoints -------------------------------------------------------------
    def _api_health(self) -> None:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        fingerprint = _hub_fingerprint(server.hub)
        manifest = _manifest(server.hub)
        self._send_json(
            200,
            {
                "ok": True,
                "version": __version__,
                "uptime_s": int(time.monotonic() - server.started_monotonic),
                "hub": {
                    "dir": str(server.hub.root),
                    "records": fingerprint["records_total"],
                    "bots": len(server.config.bots),
                    "last_sync": _manifest_last_sync(server.hub, manifest),
                },
                "redaction": _redaction_stats(server.hub),
            },
        )

    def _api_overview(self) -> None:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        analysis = server.analysis()
        self._send_json(
            200,
            {
                "generated_at": utcnow_iso(),
                "bots": [
                    server.bot_summary(bot_id, analysis) for bot_id in server.config.bots
                ],
                "counts": {
                    "records": analysis["records_total"],
                    "by_kind": dict(sorted(analysis["by_kind"].items())),
                    "attention": len(analysis["attention"]),
                },
                "activity": analysis["activity"],
                "recent": analysis["recent"],
                "attention": analysis["attention"],
            },
        )

    def _api_bots(self) -> None:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        analysis = server.analysis()
        self._send_json(
            200,
            {"items": [server.bot_summary(bot_id, analysis) for bot_id in server.config.bots]},
        )

    def _api_bot_detail(self, bot_id: str) -> None:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        if not bot_id or "/" in bot_id or bot_id not in server.config.bots:
            raise _HttpError(404, "not_found", f"unknown bot: {bot_id!r}")
        analysis = server.analysis()
        recent = [
            record.brief()
            for record in sorted(_iter_records(server.hub, bot=bot_id), key=_record_sort_key, reverse=True)[:20]
        ]
        self._send_json(
            200,
            {
                "bot": server.bot_summary(bot_id, analysis),
                "recent": recent,
                "digests": self._digest_list(bot_id),
            },
        )

    def _digest_list(self, bot_id: str) -> list[dict[str, str]]:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        try:
            raw = server.hub.list_digests(bot_id)
        except NotImplementedError:
            return []
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to list digests for %s", bot_id)
            return []
        out: list[dict[str, str]] = []
        for item in raw or []:
            text = str(item).strip()
            if not text:
                continue
            if text.endswith(".md"):
                name = Path(text).name
                date = Path(name).stem
            else:
                date = text
                name = f"{date}.md"
            out.append({"date": date, "path": f"digests/{bot_id}/{name}"})
        out.sort(key=lambda item: item["date"], reverse=True)
        return out

    def _api_digest(self, rest: str) -> None:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        parts = [part for part in rest.split("/") if part != ""]
        if len(parts) != 2:
            raise _HttpError(404, "not_found", "expected /api/digests/{bot}/{date}")
        bot_id, date = parts
        if bot_id not in server.config.bots:
            raise _HttpError(404, "not_found", f"unknown bot: {bot_id!r}")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise _HttpError(400, "bad_request", f"invalid date (expected YYYY-MM-DD): {date!r}") from None
        try:
            content = server.hub.read_digest(bot_id, date)
        except NotImplementedError:
            content = None
        if content is None:
            raise _HttpError(404, "not_found", f"no digest for {bot_id} on {date}")
        self._send_json(200, {"bot": bot_id, "date": date, "content": content})

    def _api_records(self, query: dict[str, list[str]]) -> None:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        bot = _bot_filter(query)
        kind = _text_arg(query, "kind") or None
        q = _text_arg(query, "q")
        limit = _int_arg(query, "limit", 50, 1, 500)
        offset = _int_arg(query, "offset", 0, 0, 10_000_000)
        matched = [
            record
            for record in _iter_records(server.hub, bot=bot, kind=kind)
            if not q or _matches_q(record, q)
        ]
        matched.sort(key=_record_sort_key, reverse=True)
        total = len(matched)
        page = matched[offset : offset + limit]
        self._send_json(
            200,
            {
                "items": [record.brief() for record in page],
                "total": total,
                "limit": limit,
                "offset": offset,
            },
        )

    def _api_record_detail(self, record_id: str) -> None:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        if not record_id or "/" in record_id:
            raise _HttpError(404, "not_found", f"record not found: {record_id!r}")
        record = server.hub.read_record(record_id)
        if record is None:
            raise _HttpError(404, "not_found", f"record not found: {record_id}")
        self._send_json(200, record.to_dict())

    def _api_search(self, query: dict[str, list[str]]) -> None:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        q = _text_arg(query, "q")
        if not q:
            raise _HttpError(400, "bad_request", "q is required")
        bot = _bot_filter(query)
        kind = _text_arg(query, "kind") or None
        limit = _int_arg(query, "limit", 30, 1, 200)
        # SearchIndex.search() rebuilds a missing/stale index and falls back to a linear scan
        # when FTS5 is unavailable, so the server only has to sanitise the result.
        hits = SearchIndex(server.hub).search(q, bot=bot, kind=kind, limit=limit)
        items = []
        for hit in hits:
            record = hit.get("record")
            if isinstance(record, Record):
                brief = record.brief()
            elif isinstance(record, dict):
                brief = record
            else:
                continue
            try:
                score = float(hit.get("score", 1.0))
            except (TypeError, ValueError):
                score = 1.0
            items.append({"record": brief, "score": score, "snippet": hit.get("snippet") or ""})
        self._send_json(200, {"items": items, "query": q})

    def _board(self) -> dict[str, Any]:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        return Board(server.hub).load()

    def _api_board(self) -> None:
        data = self._board()
        columns = data.get("columns") if isinstance(data, dict) else None
        if not isinstance(columns, list) or not columns:
            columns = [{"id": column_id, "title": title} for column_id, title in BOARD_COLUMNS]
        cards = []
        for card in data.get("cards") or []:
            if hasattr(card, "to_dict"):
                cards.append(card.to_dict())
            elif isinstance(card, dict):
                cards.append(card)
        self._send_json(200, {"columns": columns, "cards": cards})

    def _api_commands(self, query: dict[str, list[str]]) -> None:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        limit = _int_arg(query, "limit", 30, 1, 500)
        reader = getattr(server.hub, "iter_commands", None)
        items: list[dict[str, Any]] = []
        if callable(reader):
            try:
                for command in reader(limit=limit):
                    items.append(_command_dict(command))
            except NotImplementedError:  # pragma: no cover - alternative Hub implementations
                items = []
            except Exception:  # pragma: no cover - defensive: fall back to raw files below
                logger.exception("failed to list commands via hub")
                items = []
        if not items:
            commands_dir = Path(server.hub.root) / "commands"
            try:
                with os.scandir(commands_dir) as scan:
                    names = sorted(entry.name for entry in scan if entry.name.endswith(".json"))
            except OSError:
                names = []
            for name in names:
                data = _json_file(commands_dir / name)
                if data:
                    data.setdefault("id", Path(name).stem)
                    items.append(data)
            items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        self._send_json(200, {"items": items[:limit]})

    # -- write endpoints ------------------------------------------------------------
    def _api_board_create(self) -> None:
        body = self._read_json_body()
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            raise _HttpError(400, "bad_request", "title is required and must be a non-empty string")
        column = body.get("column", "inbox")
        if column is None:
            column = "inbox"
        if not isinstance(column, str) or column not in BOARD_COLUMN_IDS:
            raise _HttpError(400, "invalid_column", f"unknown board column: {column!r}")
        desc = body.get("desc", "")
        if desc is None:
            desc = ""
        if not isinstance(desc, str):
            raise _HttpError(400, "bad_request", "desc must be a string")
        assignee = body.get("assignee")
        if assignee is not None and not isinstance(assignee, str):
            raise _HttpError(400, "bad_request", "assignee must be a string or null")
        tags = body.get("tags", [])
        if tags is None:
            tags = []
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise _HttpError(400, "bad_request", "tags must be a list of strings")
        author = body.get("author", "human")
        if not isinstance(author, str) or not author.strip():
            author = "human"
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        try:
            card = Board(server.hub).add_card(
                title=title.strip(),
                desc=desc,
                column=column,
                assignee=assignee,
                tags=tags,
                author=author,
            )
        except ValueError as exc:
            raise _HttpError(400, "invalid_card", str(exc)) from None
        server.invalidate_analysis()
        self._send_json(201, {"card": _card_dict(card)})

    def _api_board_patch(self, card_id: str) -> None:
        if not card_id or "/" in card_id:
            raise _HttpError(404, "not_found", f"card not found: {card_id!r}")
        body = self._read_json_body()
        allowed = {"title", "desc", "column", "assignee", "tags"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise _HttpError(400, "bad_request", f"unknown field(s): {', '.join(unknown)}")
        changes: dict[str, Any] = {}
        if "title" in body:
            title = body["title"]
            if not isinstance(title, str) or not title.strip():
                raise _HttpError(400, "bad_request", "title must be a non-empty string")
            changes["title"] = title.strip()
        if "desc" in body:
            if not isinstance(body["desc"], str):
                raise _HttpError(400, "bad_request", "desc must be a string")
            changes["desc"] = body["desc"]
        if "column" in body:
            column = body["column"]
            if not isinstance(column, str) or column not in BOARD_COLUMN_IDS:
                raise _HttpError(400, "invalid_column", f"unknown board column: {column!r}")
            changes["column"] = column
        if "assignee" in body:
            assignee = body["assignee"]
            if assignee is not None and not isinstance(assignee, str):
                raise _HttpError(400, "bad_request", "assignee must be a string or null")
            changes["assignee"] = assignee
        if "tags" in body:
            tags = body["tags"]
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                raise _HttpError(400, "bad_request", "tags must be a list of strings")
            changes["tags"] = tags
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        try:
            card = Board(server.hub).update_card(card_id, **changes)
        except KeyError:
            raise _HttpError(404, "not_found", f"card not found: {card_id}") from None
        except ValueError as exc:
            raise _HttpError(400, "invalid_card", str(exc)) from None
        if card is None:
            raise _HttpError(404, "not_found", f"card not found: {card_id}")
        server.invalidate_analysis()
        self._send_json(200, {"card": _card_dict(card)})

    def _api_board_delete(self, card_id: str) -> None:
        if not card_id or "/" in card_id:
            raise _HttpError(404, "not_found", f"card not found: {card_id!r}")
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        try:
            deleted = Board(server.hub).delete_card(card_id)
        except KeyError:
            deleted = False
        if not deleted:
            raise _HttpError(404, "not_found", f"card not found: {card_id}")
        server.invalidate_analysis()
        self._send_json(200, {"ok": True})

    def _api_command_create(self) -> None:
        body = self._read_json_body()
        target_bot = body.get("target_bot")
        text = body.get("text")
        if not isinstance(target_bot, str) or not target_bot.strip():
            raise _HttpError(400, "bad_request", "target_bot is required and must be a string")
        if not isinstance(text, str) or not text.strip():
            raise _HttpError(400, "bad_request", "text is required and must be a non-empty string")
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        if target_bot not in server.config.bots:
            raise _HttpError(400, "unknown_bot", f"unknown bot: {target_bot!r}")
        try:
            command = dispatch_mod.relay(server.config, server.hub, target_bot, text.strip())
        except ValueError as exc:
            # e.g. an invalid target bot or empty text that our own validation missed
            raise _HttpError(400, "bad_request", str(exc)) from None
        except Exception as exc:
            logger.exception("command relay failed")
            raise _HttpError(502, "dispatch_failed", f"chat relay failed: {exc}") from None
        command_dict = _command_dict(command)
        if command_dict.get("status") == "failed":
            self._send_json(
                502,
                {
                    "error": {
                        "code": "dispatch_failed",
                        "message": str(command_dict.get("error") or "chat relay failed"),
                    },
                    "command": command_dict,
                },
            )
            return
        self._send_json(200, {"command": command_dict})

    def _api_sync(self) -> None:
        body = self._read_json_body(required=False)
        if not isinstance(body, dict):  # pragma: no cover - guarded by _read_json_body
            raise _HttpError(400, "invalid_json", "request body must be a JSON object")
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        server.start_sync()  # single-flight: a second trigger while running is a no-op
        self._send_json(202, {"ok": True})

    # -- SSE ------------------------------------------------------------------------
    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        if getattr(self, "_auth_cookie", False) and self.server.token:  # type: ignore[attr-defined]
            self.send_header(
                "Set-Cookie",
                f"{_TOKEN_COOKIE}={quote(self.server.token, safe='')}; Path=/; HttpOnly; SameSite=Strict",  # type: ignore[attr-defined]
            )
        self.end_headers()
        self._headers_sent = True
        self.close_connection = True
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        client: queue.Queue = queue.Queue()
        server.register_sse(client)
        try:
            self._sse_write("hello", {"ok": True})
            last_heartbeat = time.monotonic()
            while not server._stop.is_set():
                try:
                    item = client.get(timeout=1.0)
                except queue.Empty:
                    if time.monotonic() - last_heartbeat >= SSE_HEARTBEAT_S:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        last_heartbeat = time.monotonic()
                    continue
                if item is None:
                    break
                event, data = item
                self._sse_write(event, data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            server.unregister_sse(client)

    def _sse_write(self, event: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, default=str)
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    # -- static content -------------------------------------------------------------
    def _serve_static(self, path: str, query: dict[str, list[str]]) -> None:
        server: ContextBridgeServer = self.server  # type: ignore[assignment]
        relative = unquote(path.lstrip("/"))
        if relative in ("", "/"):
            relative = "index.html"
        segments = [segment for segment in relative.split("/") if segment not in ("", ".")]
        if any(segment == ".." for segment in segments):
            raise _HttpError(403, "forbidden", "path traversal is not allowed")
        if any(segment.startswith(".") for segment in segments):
            raise _HttpError(404, "not_found", f"not found: {path}")
        web_root = server.web_dir.resolve()
        try:
            candidate = (web_root / Path(*segments)).resolve()
        except OSError:  # pragma: no cover - path resolution is pure strings mostly
            raise _HttpError(404, "not_found", f"not found: {path}") from None
        if not candidate.is_relative_to(web_root):
            raise _HttpError(403, "forbidden", "path traversal is not allowed")
        if candidate.is_file():
            body = candidate.read_bytes()
            content_type = _CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
            is_html = candidate.suffix.lower() in (".html", ".htm")
            if is_html:
                self._require_auth(query, html=True)
            extra = [] if is_html else [("Cache-Control", "public, max-age=300")]
            self._send_bytes(200, body, content_type, extra)
            return
        # Unknown non-API path: SPA fallback to index.html (or the placeholder page).
        self._require_auth(query, html=True)
        index = web_root / "index.html"
        if index.is_file():
            body = index.read_bytes()
        else:
            body = _PLACEHOLDER_HTML.encode("utf-8")
        self._send_bytes(200, body, "text/html; charset=utf-8", [("Cache-Control", "no-cache")])


class _Handled(Exception):
    """Sentinel: a response (e.g. 401) has already been written."""


def _card_dict(card: Any) -> dict[str, Any]:
    if hasattr(card, "to_dict"):
        return card.to_dict()
    if isinstance(card, dict):
        return card
    raise _HttpError(500, "internal", "board returned an invalid card object")  # pragma: no cover


def _command_dict(command: Any) -> dict[str, Any]:
    if hasattr(command, "to_dict"):
        return command.to_dict()
    if isinstance(command, dict):
        return command
    raise _HttpError(500, "internal", "dispatch returned an invalid command object")  # pragma: no cover


# ---------------------------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------------------------
def create_server(config, hub: Hub | None = None, web_dir: str | Path | None = None) -> ContextBridgeServer:
    """Create (and bind) the HTTP server without starting it.

    ``hub`` defaults to the configured hub, ensured to exist. ``web_dir`` overrides the static
    console directory (useful for tests); by default the repository's ``web/`` directory is used.
    """
    if hub is None:
        hub = Hub(config)
        hub.ensure()
    static_dir = Path(web_dir) if web_dir is not None else Path(__file__).resolve().parent.parent / "web"
    server = ContextBridgeServer(
        (config.server.host, int(config.server.port)),
        _Handler,
        config=config,
        hub=hub,
        web_dir=static_dir,
    )
    logger.info("serving context bridge on http://%s:%s (hub %s)", *server.server_address[:2], hub.root)
    return server


def run_in_thread(config, hub: Hub | None = None, web_dir: str | Path | None = None) -> ContextBridgeServer:
    """Create the server and serve it from a daemon thread; returns the server object."""
    server = create_server(config, hub=hub, web_dir=web_dir)
    thread = threading.Thread(target=server.serve_forever, name="ctxbridge-http", daemon=True)
    thread.start()
    server.http_thread = thread  # type: ignore[attr-defined]
    return server


def serve(config) -> None:
    """Blocking entry point used by ``ctxbridge serve``: prints the console URL and serves."""
    server = create_server(config)
    host, port = server.server_address[0], server.server_address[1]
    url = f"http://{host}:{port}"
    if server.token:
        url += f"/?token={quote(server.token, safe='')}"
    print(f"Context Bridge console: {url}", flush=True)
    print(f"Hub: {server.hub.root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
