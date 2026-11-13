"""Collector for DSH session transcripts.

DSH stores one session per directory under ``<sessions_root>/--<cwd-slug>--/<session-id>/`` as a
zstd-compressed JSONL stream::

    session.v4.jsonl.zstd

The first line is a ``{"type": "session", "id", "createdAt", "cwd", ...}`` header; later lines are
events (``session/title``, ``agent/inbox/spliced`` with the originating request, ``turn/*``,
assistant messages, tool calls, ...). The bridge never copies transcripts. It produces a
``session`` record per session: title, originating request (truncated), outcome summary and
tool-activity counts.

Implementation notes:

- Decompress with an external ``zstd`` binary (``zstandard`` is deliberately not a runtime
  dependency). Resolution order: ``$CTXBRIDGE_ZSTD`` (absolute path or command name), then
  ``zstd`` on ``PATH``. A missing binary raises :class:`ZstdMissingError` with an actionable
  message; it never silently drops data.
- Attribute a session to a bot by matching the header's ``cwd`` against each bot's configured
  ``state_dir`` prefix (path-component aware, longest prefix wins). Sessions whose cwd matches no
  configured bot are skipped. A change to the bot registry invalidates the incremental cache.
- Incremental by ``(path, mtime_ns, size)``: unchanged transcripts are not decompressed again.
  Sessions older than a configurable window (default 30 days, ``$CTXBRIDGE_SESSION_WINDOW_DAYS``)
  are recorded in the cursor as skipped so repeated syncs stay cheap.
- Tolerate corrupt/truncated streams: unparsable lines are ignored, a whole corrupt file is
  skipped, and one bad session never aborts a sync.
- A digest is capped at ~4 KB; every string is redacted before it reaches the hub.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..model import BotSpec, Record
from ..redact import Redactor

LOG = logging.getLogger(__name__)

#: Session file name used by DSH v4.
SESSION_FILE = "session.v4.jsonl.zstd"

#: Environment variable pointing at the zstd binary (deployment hosts keep it off PATH).
ZSTD_ENV = "CTXBRIDGE_ZSTD"

#: Environment variable overriding the default "how far back" window in days.
WINDOW_ENV = "CTXBRIDGE_SESSION_WINDOW_DAYS"
DEFAULT_WINDOW_DAYS = 30

_TITLE_MAX = 300
_SUMMARY_MAX = 4096
_REQUEST_MAX = 900
_OUTCOME_MAX = 1500
_DIGEST_TOOL_NAMES = 10


class ZstdMissingError(RuntimeError):
    """The zstd command-line decoder cannot be found."""


def zstd_binary() -> str:
    """Locate the ``zstd`` decoder: ``$CTXBRIDGE_ZSTD`` first, then ``PATH``."""
    explicit = os.environ.get(ZSTD_ENV)
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(explicit)
        if found:
            return found
        raise ZstdMissingError(
            f"{ZSTD_ENV}={explicit!r} is not an executable file and not found on PATH; "
            "point it at the zstd binary or install zstd."
        )
    found = shutil.which("zstd")
    if found:
        return found
    raise ZstdMissingError(
        "zstd decoder not found: put zstd on PATH or set "
        f"{ZSTD_ENV} to its absolute path (for example in the deployment config)."
    )


def _decompress_zstd(payload: Path, binary: str) -> bytes | None:
    try:
        proc = subprocess.run(
            [binary, "-dc", "--", str(payload)],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("sessions collector: zstd failed on %s: %s", payload, exc)
        return None
    if proc.returncode != 0:
        LOG.warning(
            "sessions collector: corrupt/truncated session stream %s (zstd exit %s)",
            payload,
            proc.returncode,
        )
        return None
    return proc.stdout


def _iso_from_ms(value: Any) -> str:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return ""
    moment = datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso_from_stat(path: Path) -> str:
    st = path.stat()
    moment = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _content_text(message: dict[str, Any]) -> str:
    """Join the text blocks of an assistant/user message (ignoring reasoning/tool blocks)."""
    if not isinstance(message, dict):
        return ""
    parts: list[str] = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


def _request_text(raw: str) -> str:
    """Best-effort human-readable request from a relayed/spliced user message."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(parsed, dict):
        return raw
    trigger = parsed.get("trigger") if isinstance(parsed.get("trigger"), dict) else {}
    parts: list[str] = []
    for candidate in (
        trigger.get("text"),
        parsed.get("text"),
        parsed.get("title"),
        parsed.get("request"),
        parsed.get("instruction"),
        parsed.get("prompt"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            candidate = _one_line(candidate)
            if candidate not in parts:
                parts.append(candidate)
    return " — ".join(parts) or raw


def _canonical_path(path: str) -> str:
    """Resolve a host path for comparison: symlinked state dirs must still attribute sessions."""
    if not path:
        return ""
    expanded = os.path.expanduser(str(path))
    try:
        return os.path.realpath(expanded).rstrip("/")
    except OSError:  # pragma: no cover - realpath rarely fails; keep the plain form then
        return expanded.rstrip("/")


def _attribute_bot(cwd: str, bots: dict[str, BotSpec]) -> str | None:
    """Longest path-component prefix match of ``cwd`` against bot state dirs.

    Both sides are canonicalised first: a deployment may reach the same state directory through a
    symlinked parent, and session headers record whichever spelling the process happened to use.
    """
    canonical_cwd = _canonical_path(str(cwd or ""))
    if not canonical_cwd:
        return None
    best: str | None = None
    best_len = -1
    for bot_id, spec in bots.items():
        state_dir = _canonical_path(str(spec.state_dir or ""))
        if not state_dir:
            continue
        if (canonical_cwd == state_dir or canonical_cwd.startswith(state_dir + "/")) and len(
            state_dir
        ) > best_len:
            best, best_len = bot_id, len(state_dir)
    return best


def _window_days(override: int | None) -> int:
    if override is not None:
        return max(0, int(override))
    raw = os.environ.get(WINDOW_ENV)
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            LOG.warning("sessions collector: ignoring invalid %s=%r", WINDOW_ENV, raw)
    return DEFAULT_WINDOW_DAYS


def _config_signature(bots: dict[str, BotSpec]) -> str:
    items = sorted((bot_id, str(spec.state_dir or "")) for bot_id, spec in bots.items())
    return hashlib.sha1(json.dumps(items, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _parse_events(data: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``(header, events)`` from a decompressed JSONL stream; tolerate bad lines."""
    header: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    for index, raw_line in enumerate(data.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if header is None and event.get("type") == "session":
            header = event
            continue
        if index == 0 and header is None and "id" in event and "cwd" in event:
            header = {"type": "session", **event}
            continue
        events.append(event)
    return header or {}, events


def _session_record(
    redactor: Redactor,
    bot_spec: BotSpec,
    path: Path,
    header: dict[str, Any],
    events: list[dict[str, Any]],
) -> Record | None:
    session_id = str(header.get("id") or path.parent.name)
    title = ""
    request = ""
    outcome = ""
    tool_counts: Counter[str] = Counter()
    turns = 0
    ended_reason = ""
    model: dict[str, Any] = {}
    for event in events:
        etype = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if etype == "session/title":
            candidate = _one_line(str(data.get("title") or ""))
            if candidate:
                title = candidate
        elif etype == "user/message" and not request:
            request = _request_text(_content_text(data.get("message") or data))
        elif etype == "agent/inbox/spliced" and not request:
            inserted = data.get("inserted") or []
            if isinstance(inserted, list) and inserted and isinstance(inserted[0], dict):
                request = _request_text(_content_text(inserted[0]))
        elif etype == "assistant/message":
            text = _content_text(data.get("message") or data)
            if text:
                outcome = text
        elif etype == "tool/call":
            name = str(data.get("name") or "tool")
            tool_counts[name] += 1
        elif etype == "turn/start":
            turns += 1
        elif etype == "turn/end":
            reason = data.get("reason")
            if isinstance(reason, dict) and reason.get("kind"):
                ended_reason = str(reason["kind"])
        elif etype == "request/header" and not model:
            header_cfg = (data.get("header") or {}).get("config") or {}
            model = {"provider": header_cfg.get("provider"), "model": header_cfg.get("model")}
    if not title:
        title = _one_line(request) or f"session {session_id}"
    outcome = _truncate(outcome, _OUTCOME_MAX)
    request = _truncate(_one_line(request), _REQUEST_MAX)
    total_tools = sum(tool_counts.values())
    top_tools = ", ".join(f"{name} ×{count}" for name, count in tool_counts.most_common(_DIGEST_TOOL_NAMES))
    activity = f"{turns} turn(s) · {total_tools} tool call(s)"
    if top_tools:
        activity += f" ({top_tools})"
    if ended_reason:
        activity += f" · ended: {ended_reason}"
    lines = []
    if request:
        lines.append(f"**Request:** {request}")
    if outcome:
        lines.append(f"**Outcome:** {outcome}")
    if not outcome:
        lines.append("**Outcome:** (no final assistant text)")
    lines.append(f"**Activity:** {activity}")
    if model.get("provider") or model.get("model"):
        lines.append(f"**Model:** {model.get('provider') or '?'}/{model.get('model') or '?'}")
    summary = _truncate("\n\n".join(lines), _SUMMARY_MAX)
    preset = str(header.get("agentPreset") or "")
    tags = ["session"]
    if "worker" in preset:
        tags.append("worker")
    elif "orchestrator" in preset or "router" in preset:
        tags.append("orchestrator")
    if ended_reason and ended_reason != "completed":
        tags.append(ended_reason)
    created_at = _iso_from_ms(header.get("createdAt")) or _iso_from_stat(path)
    updated_at = _iso_from_stat(path)
    payload = {
        "session_id": session_id,
        "cwd": header.get("cwd"),
        "preset": preset or None,
        "model": model or None,
        "turns": turns,
        "tool_calls": total_tools,
        "tools": dict(tool_counts.most_common(_DIGEST_TOOL_NAMES)),
        "ended_reason": ended_reason or None,
        "events": len(events),
        "delegation_depth": header.get("delegationDepth"),
    }
    title_result = redactor.clean(_truncate(_one_line(title), _TITLE_MAX) or session_id)
    summary_result = redactor.clean(summary)
    title_clean, n_title = title_result.text, title_result.count
    summary_clean, n_summary = summary_result.text, summary_result.count
    payload_clean, n_payload = redactor.clean_obj(payload)
    source = {
        "system": "dsh-sessions",
        "profile": bot_spec.profile,
        "path": str(path),
        "source_id": session_id,
        "v": 4,
    }
    refs = {"session": session_id}
    return Record(
        bot=bot_spec.id,
        kind="session",
        title=title_clean,
        summary=summary_clean,
        created_at=created_at,
        updated_at=updated_at,
        tags=tags,
        refs=refs,
        source=source,
        payload=dict(payload_clean or {}),
        redactions=n_title + n_summary + n_payload,
    )


def _discover_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    try:
        return sorted(path for path in root.rglob(SESSION_FILE) if path.is_file())
    except OSError as exc:  # unreadable subtree: keep whatever we can
        LOG.warning("sessions collector: cannot scan %s: %s", root, exc)
        return []


def collect(
    config: Any,
    cursors: dict,
    redactor: Redactor,
    window_days: int | None = None,
) -> tuple[list[Record], dict]:
    """Digest recent DSH sessions for every configured bot.

    Returns ``(records, new_cursors)``; ``new_cursors`` is a full copy of ``cursors`` with the
    ``"sessions"`` section replaced, so it composes with other collectors.
    """
    new_cursors = deepcopy(cursors or {})
    bots: dict[str, BotSpec] = dict(config.bots or {})
    signature = _config_signature(bots)
    state = new_cursors.get("sessions")
    if not isinstance(state, dict) or state.get("config") != signature:
        state = {"v": 1, "config": signature, "window": None, "files": {}}
        new_cursors["sessions"] = state
    files_cache = state.setdefault("files", {})
    if not isinstance(files_cache, dict):
        files_cache = {}
        state["files"] = files_cache
    window = _window_days(window_days)
    if state.get("window") != window:
        # A wider window can bring previously "too old" sessions back into scope.
        for key in [k for k, entry in files_cache.items() if isinstance(entry, dict) and entry.get("old")]:
            del files_cache[key]
        state["window"] = window

    roots: list[Path] = []
    for spec in bots.values():
        root = str(spec.sessions_root or "")
        if root:
            path = Path(root).expanduser()
            if path not in roots:
                roots.append(path)

    records: list[Record] = []
    seen_paths: set[str] = set()
    if roots:
        binary: str | None = None
        try:
            binary = zstd_binary()
        except ZstdMissingError as exc:
            if any(_discover_files(root) for root in roots):
                raise ZstdMissingError(str(exc)) from exc
            binary = None
        cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=window)).timestamp() * 1000
        for root in roots:
            for path in _discover_files(root):
                key = str(path)
                seen_paths.add(key)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                cached = files_cache.get(key)
                if (
                    isinstance(cached, dict)
                    and cached.get("mtime_ns") == stat.st_mtime_ns
                    and cached.get("size") == stat.st_size
                ):
                    continue
                entry: dict[str, Any] = {
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "bot": None,
                }
                if binary is None:
                    files_cache[key] = entry
                    continue
                data = _decompress_zstd(path, binary)
                if data is None:
                    entry["error"] = "decompress"
                    files_cache[key] = entry
                    continue
                header, events = _parse_events(data)
                if not header:
                    entry["error"] = "no-header"
                    files_cache[key] = entry
                    continue
                bot_id = _attribute_bot(str(header.get("cwd") or ""), bots)
                entry["bot"] = bot_id
                if bot_id is None:
                    files_cache[key] = entry
                    continue
                created_ms = header.get("createdAt")
                try:
                    created_ok = created_ms is not None and float(created_ms) >= cutoff_ms
                except (TypeError, ValueError):
                    created_ok = False
                if not created_ok and stat.st_mtime * 1000 < cutoff_ms:
                    entry["old"] = True
                    files_cache[key] = entry
                    continue
                try:
                    record = _session_record(redactor, bots[bot_id], path, header, events)
                except Exception as exc:  # a single weird transcript must not break a sync
                    LOG.warning("sessions collector: cannot digest %s: %s", path, exc)
                    entry["error"] = "digest"
                    files_cache[key] = entry
                    continue
                files_cache[key] = entry
                if record is not None:
                    records.append(record)
    # Drop cache entries for files that disappeared so cursors.json stays bounded.
    for key in list(files_cache):
        if key not in seen_paths:
            del files_cache[key]
    return records, new_cursors
