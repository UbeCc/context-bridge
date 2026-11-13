"""Shared pytest fixtures: fresh temp hubs and configs, no network, no real bot state."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from context_bridge.config import parse_config
from context_bridge.hub import Hub


@pytest.fixture()
def config(tmp_path):
    state_a = tmp_path / "state-a"
    state_b = tmp_path / "state-b"
    state_a.mkdir()
    state_b.mkdir()
    data = {
        "hub": {"dir": str(tmp_path / "hub"), "commit": False},
        "server": {"host": "127.0.0.1", "port": 18080},
        "bots": {
            "alpha": {"label": "Alpha", "profile": "superagent", "state_dir": str(state_a)},
            "beta": {"label": "Beta", "profile": "superagent-beta", "state_dir": str(state_b)},
        },
    }
    return parse_config(data)


@pytest.fixture()
def hub(config) -> Hub:
    h = Hub(config)
    h.ensure()
    return h


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        ).stdout

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    return repo


def make_superagent_db(path: Path, rows: dict[str, list[dict]] | None = None) -> Path:
    """Create a minimal superagent-shaped sqlite database for collector tests."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("CREATE TABLE trigger (id TEXT PRIMARY KEY, value TEXT NOT NULL, at TEXT NOT NULL)")
    con.execute("CREATE TABLE disposition (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("CREATE TABLE thread (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("CREATE TABLE ticket (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("CREATE TABLE decision (id TEXT PRIMARY KEY, thread TEXT NOT NULL, value TEXT NOT NULL)")
    con.execute("CREATE TABLE trigger_thread (thread TEXT NOT NULL, id TEXT NOT NULL, at TEXT NOT NULL, human INTEGER NOT NULL, PRIMARY KEY (thread, id))")
    data = rows or {}
    for table, items in data.items():
        for item in items:
            if table == "ticket":
                con.execute("INSERT INTO ticket (id, value) VALUES (?, ?)", (item["id"], json.dumps(item["value"])))
            elif table == "trigger":
                con.execute("INSERT INTO trigger (id, value, at) VALUES (?, ?, ?)", (item["id"], json.dumps(item["value"]), item["value"].get("at", "")))
            elif table == "decision":
                con.execute("INSERT INTO decision (id, thread, value) VALUES (?, ?, ?)", (item["id"], item.get("thread", ""), json.dumps(item["value"])))
            elif table == "thread":
                con.execute("INSERT INTO thread (key, value) VALUES (?, ?)", (item["id"], json.dumps(item["value"])))
    con.commit()
    con.close()
    return path
