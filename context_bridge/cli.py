"""``ctxbridge`` command line interface.

Subcommands are thin: argument parsing and output formatting live here, behaviour lives in the
owning modules. Keep every command usable by an agent (``--json`` on read paths, non-zero exit
codes on failure, no interactive prompts unless explicitly requested).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

from . import __version__
from .board import Board
from .config import ConfigError, config_template, load_config
from .hub import Hub, HubError
from .index import SearchIndex, fts5_supported
from .model import Record, record_relpath, utcnow_iso
from .redact import Redactor


class CLIError(RuntimeError):
    """A user-facing CLI failure (not a bug): printed without a traceback, exit code 1."""


def _config_path(args) -> str:
    if getattr(args, "config", None):
        return args.config
    env = os.environ.get("CTXBRIDGE_CONFIG")
    if env:
        return env
    local = Path("config.toml")
    if local.is_file():
        return str(local)
    return str(Path.home() / ".config" / "context-bridge" / "config.toml")


def _load(args):
    return load_config(_config_path(args))


def _hub(config, *, must_exist: bool = True) -> Hub:
    hub = Hub(config)
    if must_exist and not hub.manifest_file.is_file():
        raise CLIError(
            f"hub is not initialized: {hub.root} (run `ctxbridge init --dir {hub.root}`)"
        )
    return hub


def _print(data, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return
    print(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str))


def _first_line(text: str, limit: int = 120) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _iso_before(days: int) -> str:
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _resolve_binary(value: str | None) -> str | None:
    if not value:
        return None
    candidate = Path(str(value)).expanduser()
    if candidate.is_file():
        return str(candidate)
    return shutil.which(str(value))


# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------
def cmd_init(args) -> int:
    target = Path(args.dir).expanduser()
    already = (target / "manifest.json").exists()
    if already and not args.force:
        print(f"hub already initialized: {target}", file=sys.stderr)
        return 1
    target.mkdir(parents=True, exist_ok=True)
    hub = Hub(None, root=target)  # type: ignore[arg-type]
    hub.ensure()
    written = False
    cfg = target / "config.toml"
    if not cfg.exists():
        cfg.write_text(config_template(str(target), args.port), encoding="utf-8")
        written = True
    committed = hub.commit("hub: initialize", author="ctxbridge")
    print(f"hub initialized: {target}")
    if written:
        print(f"wrote starter config: {cfg}")
    if committed:
        print("created initial git commit")
    return 0


def cmd_gen_config(args) -> int:
    print(config_template(args.hub, args.port), end="")
    return 0


def cmd_sync(args) -> int:
    from .sync import run_sync, sync_loop

    config = _load(args)
    bots = [args.bot] if args.bot else None
    hub = Hub(config)
    hub.ensure()
    if args.loop:
        sync_loop(config, args.loop, bots=bots)
        return 0
    result = run_sync(
        config, bots=bots, with_sessions=not args.no_sessions, commit=not args.no_commit
    )
    indexed = SearchIndex(hub).rebuild()
    payload = dict(result) if isinstance(result, dict) else {"result": result}
    payload["indexed"] = indexed
    _print(payload, args.json)
    return 0


def cmd_index(args) -> int:
    config = _load(args)
    hub = _hub(config)
    indexed = SearchIndex(hub).rebuild()
    _print({"indexed": indexed}, args.json)
    return 0


def cmd_search(args) -> int:
    config = _load(args)
    hub = _hub(config)
    bot = None if args.bot in (None, "all", "*") else args.bot
    if bot:
        config.bot(bot)  # validate against the configured fleet
    hits = SearchIndex(hub).search(args.query, bot=bot, kind=args.kind, limit=args.limit)
    if args.json:
        _print(
            [
                {
                    "record": hit["record"].brief(),
                    "snippet": hit.get("snippet", ""),
                    "score": hit.get("score", 1.0),
                }
                for hit in hits
            ],
            True,
        )
        return 0
    if not hits:
        print("(no matches)")
        return 0
    for hit in hits:
        record = hit["record"]
        print(f"[{record.bot}/{record.kind}] {record.title}  ({record.updated_at[:19]})")
        snippet = " ".join((hit.get("snippet") or "").split())
        if snippet:
            print(f"    {snippet[:300]}")
    return 0


def cmd_recall(args) -> int:
    config = _load(args)
    hub = _hub(config)
    bot = None if args.bot in ("all", "*") else args.bot
    if bot:
        config.bot(bot)
    cutoff = _iso_before(args.days)
    records = [
        record
        for record in hub.iter_records(bot=bot, kind=args.kind)
        if (record.updated_at or record.created_at or "")[:19] >= cutoff
    ]
    records.sort(key=lambda record: record.updated_at or record.created_at, reverse=True)
    records = records[: args.limit]
    if args.json:
        _print([record.brief() for record in records], True)
        return 0
    if not records:
        print("(nothing in range)")
        return 0
    for record in records:
        print(f"[{record.bot}/{record.kind}] {record.title}  ({record.updated_at[:19]})")
        if record.summary:
            print("    " + " ".join(record.summary.split())[:300])
    return 0


def cmd_note(args) -> int:
    config = _load(args)
    hub = _hub(config)
    bot = config.bot(args.bot).id
    redactor = Redactor(config)

    cleaned = redactor.clean(args.text)
    redactions = int(cleaned.count or 0)
    text = cleaned.text
    if not text.strip():
        raise CLIError("note text is empty after redaction")
    if args.title:
        title_result = redactor.clean(args.title)
        redactions += int(title_result.count or 0)
        title = _first_line(title_result.text.strip())
    else:
        title = _first_line(text)
    tags: list[str] = []
    for tag in args.tag or []:
        tag_result = redactor.clean(tag)
        redactions += int(tag_result.count or 0)
        cleaned_tag = tag_result.text.strip()
        if cleaned_tag:
            tags.append(cleaned_tag)

    stamp = utcnow_iso().replace("-", "").replace(":", "").replace(".", "")
    record = Record(
        bot=bot,
        kind="note",
        title=title or "note",
        summary=text,
        tags=tags,
        source={"system": "ctxbridge", "source_id": f"{stamp}-{uuid.uuid4().hex[:8]}"},
        payload={"author": bot},
        redactions=redactions,
    )
    hub.write_records([record])
    hub.commit(f"[{bot}] note: {record.title[:80]}", author=bot)
    _print(
        {
            "id": record.id,
            "bot": bot,
            "path": record_relpath(record),
            "title": record.title,
            "redactions": redactions,
        },
        args.json,
    )
    return 0


def cmd_board(args) -> int:
    config = _load(args)
    hub = _hub(config)
    board = Board(hub)
    if args.action == "list":
        data = board.load()
        if args.json:
            _print({"columns": data["columns"], "cards": [c.to_dict() for c in data["cards"]]}, True)
            return 0
        for column in data["columns"]:
            cards = [card for card in data["cards"] if card.column == column["id"]]
            print(f"== {column['title']} ({len(cards)})")
            for card in cards:
                who = f" @{card.assignee}" if card.assignee else ""
                print(f"  - {card.id}  {card.title}{who}")
        return 0
    if args.action == "add":
        card = board.add_card(
            title=args.title,
            desc=args.desc or "",
            column=args.column,
            assignee=args.assignee,
            tags=args.tag or [],
            author=args.author or "human",
        )
        _print(card.to_dict(), args.json)
        return 0
    if args.action == "move":
        card = board.update_card(args.card_id, column=args.column)
        _print(card.to_dict(), args.json)
        return 0
    if args.action == "assign":
        card = board.update_card(args.card_id, assignee=args.assignee)
        _print(card.to_dict(), args.json)
        return 0
    if args.action == "remove":
        removed = board.delete_card(args.card_id)
        _print({"ok": removed}, args.json)
        return 0 if removed else 1
    raise CLIError(f"unknown board action: {args.action}")


def cmd_serve(args) -> int:
    from .server import serve

    config = _load(args)
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    serve(config)
    return 0


def cmd_status(args) -> int:
    config = _load(args)
    data = Hub(config).status()
    data["version"] = __version__
    _print(data, args.json)
    return 0


def cmd_doctor(args) -> int:
    import sqlite3

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "", required: bool = True) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "required": required})

    check("python>=3.11", sys.version_info >= (3, 11), sys.version.split()[0] + " " + sys.executable)
    version = sqlite3.sqlite_version_info
    check(
        "python-sqlite>=3.38",
        version >= (3, 38),
        f"{sqlite3.sqlite_version} (superagent collectors need >=3.38)",
    )
    check("sqlite-fts5", fts5_supported(), "full-text search support")

    git = shutil.which("git")
    check("git", bool(git), git or "missing")

    zstd = _resolve_binary(os.environ.get("CTXBRIDGE_ZSTD")) or _resolve_binary("zstd")
    check("zstd", bool(zstd), zstd or "missing (session digests disabled)", required=False)

    config = None
    try:
        config = _load(args)
        check("config", True, str(config.path))
    except ConfigError as exc:
        check("config", False, str(exc))

    if config is not None:
        hub = Hub(config)
        check("hub-dir", hub.root.is_dir(), str(hub.root))
        check(
            "hub-initialized",
            hub.manifest_file.is_file(),
            f"{hub.manifest_file} (run `ctxbridge init --dir {hub.root}`)",
        )
        if hub.root.is_dir():
            check("hub-writable", os.access(hub.root, os.W_OK), str(hub.root))
        for bot_id, spec in config.bots.items():
            state_dir = Path(spec.state_dir)
            check(f"bot:{bot_id}:state_dir", state_dir.is_dir(), spec.state_dir)
            check(
                f"bot:{bot_id}:orchestrator.sqlite",
                (state_dir / "orchestrator.sqlite").is_file(),
                str(state_dir / "orchestrator.sqlite"),
                required=False,
            )
        check(
            "chat-relay",
            not config.feishu.enabled or bool(_resolve_binary(config.feishu.cli)),
            "enabled" if config.feishu.enabled else "disabled in config (commands are recorded, not sent)",
            required=bool(config.feishu.enabled),
        )

    ok = all(item["ok"] for item in checks if item["required"])
    _print({"ok": ok, "checks": checks}, args.json)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctxbridge", description="Shared context & memory hub for multi-bot agent fleets")
    parser.add_argument("--version", action="version", version=f"context-bridge {__version__}")
    parser.add_argument("--config", help="deployment config path (default: ./config.toml or $CTXBRIDGE_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a new hub repository")
    p.add_argument("--dir", default="./hub")
    p.add_argument("--port", type=int, default=8080, help="port written into the starter config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("gen-config", help="print a starter config.toml")
    p.add_argument("--hub", default="./hub")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_gen_config)

    p = sub.add_parser("sync", help="export bot state into the hub")
    p.add_argument("--bot")
    p.add_argument("--loop", type=int, default=0, help="run forever every N seconds")
    p.add_argument("--no-sessions", action="store_true")
    p.add_argument("--no-commit", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("index", help="rebuild the full-text search index")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", help="search shared memory")
    p.add_argument("query")
    p.add_argument("--bot", default="all")
    p.add_argument("--kind")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("recall", help="recent memory of one bot")
    p.add_argument("bot")
    p.add_argument("--days", type=int, default=2)
    p.add_argument("--kind")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("note", help="write a memory note for other bots")
    p.add_argument("text")
    p.add_argument("--bot", required=True)
    p.add_argument("--title")
    p.add_argument("--tag", action="append")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("board", help="shared kanban board")
    board_sub = p.add_subparsers(dest="action", required=True)
    b = board_sub.add_parser("list")
    b.add_argument("--json", action="store_true")
    b = board_sub.add_parser("add")
    b.add_argument("title")
    b.add_argument("--desc")
    b.add_argument("--column", default="inbox")
    b.add_argument("--assignee")
    b.add_argument("--tag", action="append")
    b.add_argument("--author")
    b.add_argument("--json", action="store_true")
    b = board_sub.add_parser("move")
    b.add_argument("card_id")
    b.add_argument("column")
    b.add_argument("--json", action="store_true")
    b = board_sub.add_parser("assign")
    b.add_argument("card_id")
    b.add_argument("assignee")
    b.add_argument("--json", action="store_true")
    b = board_sub.add_parser("remove")
    b.add_argument("card_id")
    b.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("serve", help="serve the web console and API")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("status", help="hub status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="check the deployment environment")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except HubError as exc:
        print(f"hub error: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        missing = exc.args[0] if exc.args else exc
        print(f"not found: {missing}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except NotImplementedError as exc:
        print(f"error: not implemented yet: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
