"""``ctxbridge`` command line interface.

Subcommands are thin: argument parsing and output formatting live here, behaviour lives in the
owning modules. Keep every command usable by an agent (``--json`` on read paths, non-zero exit
codes on failure, no interactive prompts unless explicitly requested).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, config_template, load_config


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


def _print(data, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return
    print(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------
def cmd_init(args) -> int:
    from .hub import Hub

    target = Path(args.dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    if (target / "manifest.json").exists() and not args.force:
        print(f"hub already initialized: {target}", file=sys.stderr)
        return 1
    hub = Hub(None, root=target)  # type: ignore[arg-type]
    hub.ensure()
    cfg = target / "config.toml"
    if not cfg.exists():
        cfg.write_text(config_template(str(target), 8080), encoding="utf-8")
        print(f"wrote starter config: {cfg}")
    print(f"hub initialized: {target}")
    return 0


def cmd_gen_config(args) -> int:
    print(config_template(args.hub, args.port), end="")
    return 0


def cmd_sync(args) -> int:
    from .sync import run_sync, sync_loop

    config = _load(args)
    bots = [args.bot] if args.bot else None
    if args.loop:
        sync_loop(config, args.loop, bots=bots)
        return 0
    result = run_sync(config, bots=bots, with_sessions=not args.no_sessions, commit=not args.no_commit)
    _print(result, args.json)
    return 0


def cmd_index(args) -> int:
    from .hub import Hub
    from .index import SearchIndex

    config = _load(args)
    idx = SearchIndex(Hub(config))
    n = idx.rebuild()
    _print({"indexed": n}, args.json)
    return 0


def cmd_search(args) -> int:
    from .hub import Hub
    from .index import SearchIndex

    config = _load(args)
    idx = SearchIndex(Hub(config))
    bot = None if args.bot in (None, "all", "*") else args.bot
    hits = idx.search(args.query, bot=bot, kind=args.kind, limit=args.limit)
    if args.json:
        _print([{"record": h["record"].brief(), "snippet": h.get("snippet", "")} for h in hits], True)
        return 0
    if not hits:
        print("(no matches)")
        return 0
    for hit in hits:
        r = hit["record"]
        print(f"[{r.bot}/{r.kind}] {r.title}  ({r.updated_at[:19]})")
        snippet = " ".join((hit.get("snippet") or "").split())
        if snippet:
            print(f"    {snippet[:300]}")
    return 0


def cmd_recall(args) -> int:
    from datetime import datetime, timedelta, timezone

    from .hub import Hub

    config = _load(args)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%S")
    records = [
        r
        for r in Hub(config).iter_records(bot=args.bot, kind=args.kind)
        if (r.updated_at or r.created_at) >= cutoff
    ]
    records.sort(key=lambda r: r.updated_at or r.created_at, reverse=True)
    records = records[: args.limit]
    if args.json:
        _print([r.brief() for r in records], True)
        return 0
    if not records:
        print("(nothing in range)")
        return 0
    for r in records:
        print(f"[{r.kind}] {r.title}  ({r.updated_at[:19]})")
        if r.summary:
            print("    " + " ".join(r.summary.split())[:300])
    return 0


def cmd_note(args) -> int:
    from .hub import Hub
    from .model import Record, utcnow_iso

    config = _load(args)
    hub = Hub(config)
    record = Record(
        bot=args.bot,
        kind="note",
        title=args.text.splitlines()[0][:120],
        summary=args.text,
        tags=args.tag or [],
        source={"system": "ctxbridge", "source_id": utcnow_iso()},
        payload={"author": args.bot},
    )
    hub.write_records([record])
    hub.commit(f"[{args.bot}] note: {record.title[:80]}")
    _print({"id": record.id, "path": f"records/{record.bot}/note/{record.id}.json"}, args.json)
    return 0


def cmd_board(args) -> int:
    from .hub import Hub
    from .board import Board

    config = _load(args)
    board = Board(Hub(config))
    if args.action == "list":
        data = board.load()
        if args.json:
            _print({"columns": data["columns"], "cards": [c.to_dict() for c in data["cards"]]}, True)
            return 0
        for col in data["columns"]:
            cards = [c for c in data["cards"] if c.column == col["id"]]
            print(f"== {col['title']} ({len(cards)})")
            for c in cards:
                who = f" @{c.assignee}" if c.assignee else ""
                print(f"  - {c.id}  {c.title}{who}")
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
        ok = board.delete_card(args.card_id)
        _print({"ok": ok}, args.json)
        return 0 if ok else 1
    raise SystemExit(f"unknown board action: {args.action}")


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
    from .hub import Hub

    config = _load(args)
    data = Hub(config).status()
    data["version"] = __version__
    _print(data, args.json)
    return 0


def cmd_doctor(args) -> int:
    import shutil
    import sqlite3

    from .hub import Hub

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    try:
        config = _load(args)
        check("config", True, str(config.path))
        hub = Hub(config)
        check("hub", hub.root.is_dir(), str(hub.root))
        for bot_id, spec in config.bots.items():
            check(f"bot:{bot_id}:state_dir", Path(spec.state_dir).is_dir(), spec.state_dir)
    except ConfigError as exc:
        check("config", False, str(exc))
        config = None

    v = sqlite3.sqlite_version_info
    check("python-sqlite>=3.38", v >= (3, 38), sqlite3.sqlite_version)
    check("zstd", bool(shutil.which("zstd")), shutil.which("zstd") or "missing (session digests disabled)")
    check("git", bool(shutil.which("git")), shutil.which("git") or "missing")
    if config is not None and config.feishu.enabled:
        check("lark-cli", bool(shutil.which(config.feishu.cli)), config.feishu.cli)

    ok = all(c["ok"] for c in checks)
    _print({"ok": ok, "checks": checks}, args.json)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctxbridge", description="Shared context & memory hub for multi-bot agent fleets")
    parser.add_argument("--version", action="version", version=f"context-bridge {__version__}")
    parser.add_argument("--config", help="deployment config path (default: ./config.toml or $CTXBRIDGE_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a new hub repository")
    p.add_argument("--dir", default="./hub")
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
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
