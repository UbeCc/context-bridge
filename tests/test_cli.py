"""End-to-end CLI tests (``main(argv)``) against temporary hubs.

No network: ``init`` creates a local git repo, commands operate on temp paths only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from context_bridge.cli import main
from context_bridge.config import load_config
from context_bridge.hub import Hub
from context_bridge.model import Record


def _json_output(capsys) -> object:
    return json.loads(capsys.readouterr().out)


def _init_hub(tmp_path: Path, capsys=None) -> tuple[Path, Path]:
    hub_dir = tmp_path / "hub"
    assert main(["init", "--dir", str(hub_dir)]) == 0
    if capsys is not None:  # discard init chatter so JSON assertions see one command's output
        capsys.readouterr()
    config_path = hub_dir / "config.toml"
    assert config_path.is_file()
    return hub_dir, config_path


def _write_config(tmp_path: Path, hub_dir: Path) -> Path:
    state_a = tmp_path / "state-a"
    state_b = tmp_path / "state-b"
    state_a.mkdir(exist_ok=True)
    state_b.mkdir(exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[hub]",
                f'dir = "{hub_dir}"',
                "commit = false",
                "",
                "[server]",
                'host = "127.0.0.1"',
                "port = 18080",
                "",
                "[bots.alpha]",
                'label = "Alpha"',
                'profile = "superagent"',
                f'state_dir = "{state_a}"',
                "",
                "[bots.beta]",
                'label = "Beta"',
                'profile = "superagent-beta"',
                f'state_dir = "{state_b}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------------------------
# init / gen-config / config errors
# ---------------------------------------------------------------------------------------------
def test_init_creates_hub_and_config(tmp_path, capsys) -> None:
    hub_dir = tmp_path / "hub"
    assert main(["init", "--dir", str(hub_dir)]) == 0
    out = capsys.readouterr().out
    assert "hub initialized" in out
    assert (hub_dir / "manifest.json").is_file()
    assert (hub_dir / ".git").is_dir()
    assert (hub_dir / "config.toml").is_file()
    # Running init again refuses without --force.
    assert main(["init", "--dir", str(hub_dir)]) == 1
    assert main(["init", "--dir", str(hub_dir), "--force"]) == 0


def test_gen_config_prints_template(capsys) -> None:
    assert main(["gen-config", "--hub", "/srv/context-hub", "--port", "9000"]) == 0
    out = capsys.readouterr().out
    assert "[hub]" in out
    assert "/srv/context-hub" in out
    assert "port = 9000" in out


def test_missing_config_is_a_config_error(tmp_path) -> None:
    assert main(["--config", str(tmp_path / "nope.toml"), "status"]) == 2


def test_uninitialized_hub_reads_fail_cleanly(tmp_path) -> None:
    config_path = _write_config(tmp_path, tmp_path / "missing-hub")
    assert main(["--config", str(config_path), "search", "bridge"]) == 1
    assert main(["--config", str(config_path), "note", "hello", "--bot", "alpha"]) == 1
    # status is diagnostic and works on an empty/missing hub.
    assert main(["--config", str(config_path), "status", "--json"]) == 0


# ---------------------------------------------------------------------------------------------
# note / index / search / recall / status
# ---------------------------------------------------------------------------------------------
def test_note_then_index_search_recall_status(tmp_path, capsys) -> None:
    hub_dir, config_path = _init_hub(tmp_path, capsys)
    assert main(["--config", str(config_path), "note", "Found the bridge service on port 8080", "--bot", "opus", "--tag", "infra", "--json"]) == 0
    note = _json_output(capsys)
    assert note["id"].startswith("opus:note:")
    assert note["path"].startswith("records/opus/note/")
    assert note["redactions"] == 0

    # A secret in note text must be masked before it reaches the hub.
    # Build a secret-shaped sample by concatenation so the public-safety scanner (which
    # scans tracked test files for credential shapes) does not flag this test fixture.
    secret = "s" + "k-" + "abcdefghijklmnop12345"
    assert main(["--config", str(config_path), "note", f"Deploy token {secret} for bridge", "--bot", "opus", "--json"]) == 0
    secret_note = _json_output(capsys)
    assert secret_note["redactions"] >= 1
    config = load_config(config_path)
    stored = Hub(config).read_record(secret_note["id"])
    assert stored is not None
    assert secret not in stored.summary
    assert "[REDACTED]" in stored.summary

    assert main(["--config", str(config_path), "index", "--json"]) == 0
    assert _json_output(capsys)["indexed"] >= 2

    assert main(["--config", str(config_path), "search", "bridge", "--json"]) == 0
    hits = _json_output(capsys)
    assert isinstance(hits, list) and hits
    assert all(set(hit) == {"record", "snippet", "score"} for hit in hits)
    assert any("<b>" in hit["snippet"] for hit in hits)

    assert main(["--config", str(config_path), "recall", "opus", "--days", "2", "--json"]) == 0
    recalled = _json_output(capsys)
    assert len(recalled) >= 2

    assert main(["--config", str(config_path), "status", "--json"]) == 0
    status = _json_output(capsys)
    assert status["initialized"] is True
    assert status["records"] >= 2
    assert "opus" in status["bots"]
    assert status["version"]
    assert hub_dir.is_dir()


def test_search_without_matches_prints_hint(tmp_path, capsys) -> None:
    _hub, config_path = _init_hub(tmp_path, capsys)
    assert main(["--config", str(config_path), "search", "zzz-no-such-term"]) == 0
    assert "(no matches)" in capsys.readouterr().out


def test_note_rejects_unknown_bot(tmp_path) -> None:
    _hub, config_path = _init_hub(tmp_path)
    assert main(["--config", str(config_path), "note", "hello", "--bot", "ghost"]) == 2


# ---------------------------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------------------------
def test_board_cli_crud(tmp_path, capsys) -> None:
    _hub_dir, config_path = _init_hub(tmp_path, capsys)
    assert main(["--config", str(config_path), "board", "add", "Review bridge PR", "--column", "review", "--assignee", "opus", "--json"]) == 0
    card = _json_output(capsys)
    assert card["column"] == "review" and card["assignee"] == "opus"
    card_id = card["id"]

    assert main(["--config", str(config_path), "board", "list", "--json"]) == 0
    board = _json_output(capsys)
    assert [column["id"] for column in board["columns"]] == ["inbox", "in_progress", "review", "done"]
    assert [item["id"] for item in board["cards"]] == [card_id]

    assert main(["--config", str(config_path), "board", "move", card_id, "done", "--json"]) == 0
    assert _json_output(capsys)["column"] == "done"
    assert main(["--config", str(config_path), "board", "assign", card_id, "beta", "--json"]) == 0
    assert _json_output(capsys)["assignee"] == "beta"

    assert main(["--config", str(config_path), "board", "remove", card_id, "--json"]) == 0
    assert _json_output(capsys) == {"ok": True}
    assert main(["--config", str(config_path), "board", "remove", card_id, "--json"]) == 1


def test_board_rejects_unknown_column_cli(tmp_path) -> None:
    _hub_dir, config_path = _init_hub(tmp_path)
    assert main(["--config", str(config_path), "board", "add", "bad", "--column", "nope"]) == 1


# ---------------------------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------------------------
def test_doctor_reports_checks_without_crashing(tmp_path, capsys) -> None:
    _hub_dir, config_path = _init_hub(tmp_path, capsys)
    code = main(["--config", str(config_path), "doctor", "--json"])
    report = _json_output(capsys)
    assert code in (0, 1)
    assert isinstance(report["checks"], list) and report["checks"]
    names = {check["check"] for check in report["checks"]}
    assert {"config", "git", "python-sqlite>=3.38", "sqlite-fts5", "hub-initialized"} <= names
    config_check = next(check for check in report["checks"] if check["check"] == "config")
    assert config_check["ok"] is True


# ---------------------------------------------------------------------------------------------
# sync (collector dependency faked: no real bot state in unit tests)
# ---------------------------------------------------------------------------------------------
def test_sync_runs_collector_then_rebuilds_index(tmp_path, capsys, monkeypatch) -> None:
    _hub_dir, config_path = _init_hub(tmp_path, capsys)
    calls: dict = {}

    def fake_run_sync(config, bots=None, with_sessions=True, commit=True):
        calls.update(bots=bots, with_sessions=with_sessions, commit=commit)
        hub = Hub(config)
        hub.ensure()
        hub.write_records(
            [
                Record(
                    bot="opus",
                    kind="note",
                    title="Synced note about the bridge",
                    summary="bridge appears here",
                    source={"source_id": "sync-1"},
                )
            ]
        )
        return {"records": 1, "per_bot": {"opus": 1}, "committed": False, "at": "2026-11-13T21:00:00Z"}

    monkeypatch.setattr("context_bridge.sync.run_sync", fake_run_sync)
    assert main(["--config", str(config_path), "sync", "--json"]) == 0
    result = _json_output(capsys)
    assert result["records"] == 1
    assert result["indexed"] == 1
    assert calls["with_sessions"] is True and calls["commit"] is True

    assert main(["--config", str(config_path), "search", "synced", "--json"]) == 0
    hits = _json_output(capsys)
    assert [hit["record"]["title"] for hit in hits] == ["Synced note about the bridge"]
