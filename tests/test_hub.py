"""Tests for the hub store, search index, board and dispatch (hub/CLI owner).

Everything runs against temporary hubs; no network, no real bot state.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess

import pytest

from context_bridge import dispatch
from context_bridge.board import Board
from context_bridge.hub import Hub
from context_bridge.index import SearchIndex
from context_bridge.model import Command, Record, make_id


def _record(bot: str = "alpha", kind: str = "note", source_id: str = "r1", **kwargs) -> Record:
    defaults = {"title": "A shared memory", "summary": "bridge details", "source": {"source_id": source_id}}
    defaults.update(kwargs)
    return Record(bot=bot, kind=kind, **defaults)


# ---------------------------------------------------------------------------------------------
# hub: lifecycle, records, metadata
# ---------------------------------------------------------------------------------------------
def test_ensure_creates_layout(hub: Hub) -> None:
    assert hub.root.is_dir()
    assert (hub.root / ".git").is_dir()
    assert hub.manifest_file.is_file()
    assert hub.board_file.is_file()
    assert hub.cursors_file.is_file()
    assert hub.redaction_file.is_file()
    for directory in (hub.records_dir, hub.digests_dir, hub.commands_dir, hub.state_dir, hub.index_dir):
        assert directory.is_dir()
    manifest = hub.read_manifest()
    assert set(manifest["bots"]) == {"alpha", "beta"}
    assert manifest["bots"]["alpha"]["label"] == "Alpha"
    # The hub repo must not be able to commit live config or caches.
    gitignore = (hub.root / ".gitignore").read_text(encoding="utf-8")
    assert "config.toml" in gitignore
    assert "index/" in gitignore


def test_record_write_read_roundtrip(hub: Hub) -> None:
    record = _record(title="Bridge design", tags=["bridge", "docs"])
    path = hub.write_record(record)
    assert path == hub.root / "records/alpha/note" / f"{record.id}.json"
    loaded = hub.read_record(record.id)
    assert loaded is not None
    assert loaded.title == "Bridge design"
    assert loaded.tags == ["bridge", "docs"]
    assert loaded.to_dict() == record.to_dict()
    # JSON on disk is a plain dict, not a dataclass repr.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["id"] == record.id
    assert on_disk["v"] == 1


def test_record_overwrite_is_atomic_and_leaves_no_temp_files(hub: Hub) -> None:
    first = _record(title="first version")
    hub.write_records([first])
    second = _record(title="second version")
    assert second.id == first.id
    hub.write_records([second])
    loaded = hub.read_record(first.id)
    assert loaded is not None and loaded.title == "second version"
    assert not list(hub.records_dir.rglob("*.tmp"))
    assert len(list(hub.records_dir.rglob("*.json"))) == 1


def test_iter_records_filters(hub: Hub) -> None:
    hub.write_records(
        [
            _record(bot="alpha", kind="note", source_id="a1"),
            _record(bot="alpha", kind="ticket", source_id="a2"),
            _record(bot="beta", kind="note", source_id="b1"),
        ]
    )
    assert {record.bot for record in hub.iter_records()} == {"alpha", "beta"}
    assert [record.kind for record in hub.iter_records(bot="alpha")] == ["note", "ticket"]
    assert [record.id for record in hub.iter_records(kind="note")] == [
        make_id("alpha", "note", "a1"),
        make_id("beta", "note", "b1"),
    ]
    assert list(hub.iter_records(bot="ghost")) == []


def test_read_record_missing_and_malformed(hub: Hub) -> None:
    assert hub.read_record("") is None
    assert hub.read_record("not-an-id") is None
    assert hub.read_record("alpha:note:missing") is None
    broken = hub.records_dir / "alpha" / "note" / "alpha:note:broken.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not json", encoding="utf-8")
    assert hub.read_record("alpha:note:broken") is None
    assert list(hub.iter_records()) == []


def test_digest_roundtrip_and_listing(hub: Hub) -> None:
    path = hub.write_digest("alpha", "2026-11-13", "# Alpha · 2026-11-13")
    assert path == hub.root / "digests" / "alpha" / "2026-11-13.md"
    assert hub.read_digest("alpha", "2026-11-13") == "# Alpha · 2026-11-13\n"
    hub.write_digest("alpha", "2026-11-12", "older")
    assert hub.list_digests("alpha") == ["2026-11-13", "2026-11-12"]
    assert hub.list_digests("beta") == []
    assert hub.read_digest("alpha", "1999-01-01") is None


def test_manifest_cursors_and_redaction_stats(hub: Hub) -> None:
    hub.write_manifest({"last_sync": {"at": "2026-11-13T21:00:00Z", "records": 5}})
    manifest = hub.read_manifest()
    assert manifest["last_sync"]["at"] == "2026-11-13T21:00:00Z"
    assert set(manifest["bots"]) == {"alpha", "beta"}  # merge keeps the registry
    assert json.loads((hub.state_dir / "last-sync.json").read_text())["at"] == "2026-11-13T21:00:00Z"
    hub.write_manifest({"custom": 42})
    assert hub.read_manifest()["custom"] == 42
    assert hub.read_manifest()["last_sync"]["records"] == 5

    hub.write_cursors({"superagent": {"alpha": {"updated_at": "2026-11-13"}}})
    assert hub.read_cursors()["superagent"]["alpha"]["updated_at"] == "2026-11-13"
    hub.write_redaction_stats({"total": 3, "by_rule": {"openai_key": 3}})
    assert hub.read_redaction_stats()["by_rule"]["openai_key"] == 3


def test_command_read_write(hub: Hub) -> None:
    command = Command(target_bot="beta", text="check the bridge", status="sent", message_id="om_x")
    path = hub.write_command(command)
    assert path.is_file()
    loaded = hub.read_command(command.id)
    assert loaded is not None and loaded.target_bot == "beta" and loaded.status == "sent"
    second = Command(target_bot="alpha", text="another")
    hub.write_command(second)
    assert [c.id for c in hub.iter_commands()][0] == second.id
    assert len(hub.iter_commands(limit=1)) == 1


# ---------------------------------------------------------------------------------------------
# hub: locks and git
# ---------------------------------------------------------------------------------------------
def test_lock_is_exclusive_and_reentrant(hub: Hub) -> None:
    lock_path = hub.lock_file
    with hub.lock():
        assert lock_path.exists()
        other = os.open(str(lock_path), os.O_RDWR)
        try:
            with pytest.raises(OSError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)
        with hub.lock():  # same thread may nest (read-modify-write helpers do this)
            pass
    # Released: another open file description can take the lock right away.
    other = os.open(str(lock_path), os.O_RDWR)
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_UN)
    finally:
        os.close(other)


def test_lock_times_out_when_held_elsewhere(hub: Hub) -> None:
    other = os.open(str(hub.lock_file), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(other, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError):
            with hub.lock(timeout=0.1):
                pass
    finally:
        fcntl.flock(other, fcntl.LOCK_UN)
        os.close(other)


def test_commit_creates_git_commit_only_when_dirty(git_repo, config) -> None:
    config.hub.dir = str(git_repo)
    config.hub.commit = True
    hub = Hub(config)
    hub.ensure()
    hub.write_records([_record(source_id="commit-1")])
    assert hub.commit("hub: test commit", author="alpha") is True
    assert hub.commit("hub: test commit", author="alpha") is False
    log = subprocess.run(
        ["git", "log", "--format=%an %s"], cwd=git_repo, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert log[0].startswith("alpha ")
    assert "hub: test commit" in log[0]
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True, check=True
    ).stdout
    assert status.strip() == ""


def test_commit_respects_commit_disabled(hub: Hub) -> None:
    hub.write_records([_record()])
    assert hub.commit("should not commit") is False
    assert hub.status()["last_commit"] is None


def test_pull_push_are_noops_without_remote(hub: Hub) -> None:
    hub.pull_rebase()
    hub.push()


def test_status_counts(hub: Hub) -> None:
    hub.write_records(
        [
            _record(bot="alpha", kind="note", source_id="s1"),
            _record(bot="alpha", kind="ticket", source_id="s2"),
            _record(bot="beta", kind="session", source_id="s3"),
        ]
    )
    hub.write_digest("alpha", "2026-11-13", "digest")
    hub.write_command(Command(target_bot="beta", text="hello"))
    Board(hub).add_card(title="Review bridge", column="review")
    hub.write_manifest({"last_sync": {"at": "2026-11-13T21:00:00Z"}})
    status = hub.status()
    assert status["initialized"] is True
    assert status["records"] == 3
    assert status["by_bot"] == {"alpha": 2, "beta": 1}
    assert status["by_kind"] == {"note": 1, "ticket": 1, "session": 1}
    assert status["board_cards"] == 1
    assert status["commands"] == 1
    assert status["digests"] == 1
    assert status["last_sync"] == "2026-11-13T21:00:00Z"
    assert status["last_commit"] is None
    assert status["dir"] == str(hub.root)


# ---------------------------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------------------------
def test_board_crud(hub: Hub) -> None:
    board = Board(hub)
    empty = board.load()
    assert [column["id"] for column in empty["columns"]] == ["inbox", "in_progress", "review", "done"]
    assert empty["cards"] == []

    card = board.add_card(title="Review bridge PR", column="review", assignee="alpha", tags=["bridge"])
    loaded = board.load()["cards"]
    assert len(loaded) == 1 and loaded[0].id == card.id
    assert loaded[0].column == "review"

    moved = board.update_card(card.id, column="done", assignee="beta")
    assert moved.column == "done" and moved.assignee == "beta"
    assert board.load()["cards"][0].column == "done"

    assert board.delete_card(card.id) is True
    assert board.delete_card(card.id) is False
    assert board.load()["cards"] == []


def test_board_rejects_unknown_column_and_missing_card(hub: Hub) -> None:
    board = Board(hub)
    with pytest.raises(ValueError):
        board.add_card(title="bad", column="nope")
    with pytest.raises(ValueError):
        board.update_card("c_missing", column="nope")
    with pytest.raises(KeyError):
        board.update_card("c_missing", column="done")
    with pytest.raises(ValueError):
        board.update_card("c_missing", nonsense="x")
    with pytest.raises(ValueError):
        board.add_card(title="   ")


def test_board_card_ids_are_unique_within_one_process(hub: Hub) -> None:
    board = Board(hub)
    ids = [board.add_card(title=f"card {index}").id for index in range(5)]
    assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------------------------
# search index
# ---------------------------------------------------------------------------------------------
def _seed_search_records(hub: Hub) -> None:
    hub.write_records(
        [
            _record(
                bot="alpha",
                kind="note",
                source_id="s1",
                title="Context bridge design",
                summary="A shared memory hub over git for many bots",
            ),
            _record(
                bot="beta",
                kind="ticket",
                source_id="s2",
                title="Deploy the bridge service",
                summary="ops work on the hub host",
            ),
            _record(bot="alpha", kind="session", source_id="s3", title="Unrelated", summary="nothing useful"),
        ]
    )


def test_index_rebuild_and_search(hub: Hub) -> None:
    _seed_search_records(hub)
    index = SearchIndex(hub)
    assert index.is_stale() is True
    assert index.rebuild() == 3
    assert index.is_stale() is False

    hits = index.search("bridge")
    assert len(hits) == 2
    assert {hit["record"].bot for hit in hits} <= {"alpha", "beta"}
    assert all(0 < hit["score"] <= 1.0 for hit in hits)
    assert all("<b>bridge</b>" in hit["snippet"].lower() for hit in hits)

    assert [hit["record"].kind for hit in index.search("bridge", bot="beta")] == ["ticket"]
    assert [hit["record"].kind for hit in index.search("bridge", kind="note")] == ["note"]
    assert len(index.search("bridge", limit=1)) == 1
    assert index.search("bridge", bot="ghost") == []
    assert index.search("zzz-no-such-term") == []
    assert index.search("") == []


def test_index_detects_staleness_after_new_record(hub: Hub) -> None:
    _seed_search_records(hub)
    index = SearchIndex(hub)
    index.rebuild()
    assert index.is_stale() is False
    hub.write_records([_record(source_id="late", title="Late arriving memory", summary="bridge again")])
    assert index.is_stale() is True
    assert len(index.search("late")) == 1  # search rebuilds a stale index on demand
    assert index.is_stale() is False


def test_index_linear_fallback_without_fts(hub: Hub) -> None:
    _seed_search_records(hub)
    index = SearchIndex(hub, use_fts=False)
    assert index.rebuild() == 0
    hits = index.search("bridge")
    assert len(hits) == 2
    assert all("<b>bridge</b>" in hit["snippet"].lower() for hit in hits)
    assert [hit["record"].bot for hit in index.search("bridge", bot="alpha")] == ["alpha"]
    assert index.search("zzz-no-such-term") == []


def test_index_cjk_search(hub: Hub) -> None:
    hub.write_records(
        [
            _record(
                bot="alpha",
                kind="note",
                source_id="cjk-1",
                title="共享桥接记忆",
                summary="这个桥接服务把多个智能体的记忆汇总到一个 git 仓库",
            )
        ]
    )
    index = SearchIndex(hub)
    hits = index.search("桥接")
    assert len(hits) == 1
    assert hits[0]["record"].id == make_id("alpha", "note", "cjk-1")


# ---------------------------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------------------------
def _enable_relay(config, target_open_id: str | None = "ou_EXAMPLE_BETA") -> None:
    config.feishu.enabled = True
    config.feishu.cli = "lark-cli"
    config.feishu.identity = "bridge-bot"
    config.feishu.relay_chat_id = "oc_EXAMPLE"
    config.feishu.targets = {"beta": {"open_id": target_open_id}} if target_open_id else {}


def _runner(returncode: int = 0, stdout: str = "", stderr: str = ""):
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), returncode, stdout=stdout, stderr=stderr)

    return run, calls


def test_relay_success_records_command(hub: Hub, config) -> None:
    _enable_relay(config)
    run, calls = _runner(stdout='{"ok": true, "data": {"message_id": "om_test_1"}}')
    command = dispatch.relay(config, hub, "beta", "check the bridge hub and summarize opus memory", runner=run)
    assert command.status == "sent"
    assert command.message_id == "om_test_1"
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "lark-cli"
    assert argv[argv.index("--chat-id") + 1] == "oc_EXAMPLE"
    content = json.loads(argv[argv.index("--content") + 1])
    assert '<at user_id="ou_EXAMPLE_BETA">Beta</at>' in content["text"]
    assert "summarize opus memory" in content["text"]
    # Recorded twice: the command file and a searchable memory record.
    assert hub.read_command(command.id) is not None
    record = hub.read_record(f"beta:command:{command.id}")
    assert record is not None and record.kind == "command" and record.payload["status"] == "sent"


def test_relay_failure_records_error_without_raising(hub: Hub, config) -> None:
    _enable_relay(config)
    run, _calls = _runner(returncode=1, stderr="Bot/User can NOT be out of the chat")
    command = dispatch.relay(config, hub, "beta", "do something", runner=run)
    assert command.status == "failed"
    assert command.message_id is None
    assert "out of the chat" in (command.error or "")
    assert hub.read_command(command.id) is not None
    assert hub.read_command(command.id).status == "failed"


def test_relay_handles_ok_false_payload(hub: Hub, config) -> None:
    _enable_relay(config)
    run, _calls = _runner(stdout='{"ok": false, "error": {"message": "invalid chat id"}}')
    command = dispatch.relay(config, hub, "beta", "do something", runner=run)
    assert command.status == "failed"
    assert "invalid chat id" in (command.error or "")


def test_relay_dry_run_does_not_call_runner(hub: Hub, config, monkeypatch) -> None:
    _enable_relay(config)
    monkeypatch.setenv(dispatch.DRY_RUN_ENV, "1")
    run, calls = _runner(stdout='{"ok": true, "data": {"message_id": "om_never"}}')
    command = dispatch.relay(config, hub, "beta", "dry run instruction", runner=run)
    assert calls == []
    assert command.status == "sent"
    assert command.message_id == "dry-run"
    assert hub.read_command(command.id) is not None


def test_relay_skipped_when_disabled(hub: Hub, config) -> None:
    config.feishu.enabled = False
    command = dispatch.relay(config, hub, "beta", "not sent anywhere")
    assert command.status == "skipped"
    assert hub.read_command(command.id) is not None


def test_relay_mention_falls_back_to_plain_at_name(hub: Hub, config) -> None:
    _enable_relay(config, target_open_id=None)
    argv = dispatch.build_send_argv(config, "beta", "hello")
    content = json.loads(argv[argv.index("--content") + 1])
    assert content["text"] == "@Beta hello"


def test_relay_validates_target_and_text(hub: Hub, config) -> None:
    _enable_relay(config)
    with pytest.raises(ValueError):
        dispatch.relay(config, hub, "ghost", "hello")
    with pytest.raises(ValueError):
        dispatch.relay(config, hub, "beta", "   ")
    assert dispatch.relay_enabled(config) is True
    config.feishu.enabled = False
    assert dispatch.relay_enabled(config) is False
