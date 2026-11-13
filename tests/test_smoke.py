"""Smoke tests for the frozen contracts: model serialization, config parsing, hub init."""

from __future__ import annotations

import json

from context_bridge import __version__
from context_bridge.config import ConfigError, parse_config
from context_bridge.model import BoardCard, Record, make_id, record_relpath


def test_version() -> None:
    assert __version__


def test_record_roundtrip() -> None:
    r = Record(bot="alpha", kind="note", title="hello", summary="world", source={"source_id": "1"})
    data = r.to_dict()
    assert data["id"] == make_id("alpha", "note", "1")
    assert record_relpath(r) == f"records/alpha/note/{r.id}.json"
    assert Record.from_dict(json.loads(json.dumps(data))).title == "hello"


def test_record_id_is_stable() -> None:
    a = Record(bot="alpha", kind="ticket", title="x", source={"source_id": "abc"})
    b = Record(bot="alpha", kind="ticket", title="y", source={"source_id": "abc"})
    assert a.id == b.id


def test_board_card_defaults() -> None:
    card = BoardCard(title="do the thing")
    assert card.column == "inbox"
    assert card.id.startswith("c_")


def test_board_card_rejects_unknown_column() -> None:
    import pytest

    with pytest.raises(ValueError):
        BoardCard(title="x", column="nope")


def test_parse_config_minimal() -> None:
    cfg = parse_config(
        {
            "hub": {"dir": "/tmp/hub"},
            "bots": {"alpha": {"state_dir": "/tmp/state-a"}},
        }
    )
    assert cfg.bots["alpha"].label == "Alpha"
    assert cfg.server.port == 8080


def test_parse_config_rejects_unknown_bot_key() -> None:
    import pytest

    with pytest.raises(ConfigError):
        parse_config({"bots": {"alpha": {"state_dir": "/tmp/a", "typo": 1}}})
