"""HTTP API tests: start the real server on an ephemeral loopback port against a temp hub.

No network beyond ``127.0.0.1``; every fixture uses ``tmp_path``. Static assertions use a minimal
temporary web directory, never the frontend teammate's ``web/`` files.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from context_bridge.board import Board
from context_bridge.model import Record, utcnow_iso
from context_bridge.server import create_server, run_in_thread

# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
INDEX_HTML = '<!doctype html><html><head><title>Console</title></head><body><div id="app">CONSOLE_MARKER</div><script src="/app.js"></script></body></html>'


def iso(offset_seconds: int = 0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def request(server, method: str, path: str, *, body=None, raw: bytes | None = None,
            token: str | None = None, headers: dict[str, str] | None = None,
            timeout: float = 10.0):
    """Perform one HTTP request and return ``(status, parsed_body, headers)``."""
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}{path}"
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    request_headers = dict(headers or {})
    if data is not None:
        request_headers.setdefault("Content-Type", "application/json")
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status, raw_body, response_headers = response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        status, raw_body, response_headers = error.code, error.read(), error.headers
    try:
        parsed = json.loads(raw_body)
    except ValueError:
        parsed = raw_body
    return status, parsed, response_headers


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@contextlib.contextmanager
def served(config, hub, web_dir, token: str = ""):
    """Start ``create_server`` on port 0 in a daemon thread, tear it down afterwards."""
    config.server.port = 0
    config.server.token = token
    server = create_server(config, hub=hub, web_dir=web_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def read_sse_event(response):
    """Read the next non-comment SSE event from an ``http.client`` response.

    The response socket carries a timeout (set on the connection) so a silent stream raises
    ``TimeoutError`` instead of blocking the test run forever.
    """
    event: str | None = None
    while True:
        line = response.readline()
        if not line:
            raise AssertionError("SSE stream closed before an event arrived")
        text = line.decode("utf-8").rstrip("\r\n")
        if text.startswith("event: "):
            event = text[len("event: "):]
        elif text.startswith("data: "):
            return event, json.loads(text[len("data: "):])
        # blank separator lines and ": heartbeat" comments fall through


# ---------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------
@pytest.fixture()
def web_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "web"
    directory.mkdir()
    (directory / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (directory / "app.js").write_text("window.console.log('app');", encoding="utf-8")
    (directory / "styles.css").write_text("body { color: #fff; }", encoding="utf-8")
    # A file outside web/ that a traversal attempt must never reach.
    (tmp_path / "secret.txt").write_text("TOP-SECRET-NOT-SERVED", encoding="utf-8")
    return directory


@pytest.fixture()
def dataset(hub, config) -> dict:
    """Populate the temp hub with deterministic records, a board card, a digest and stats."""
    records = [
        Record(
            bot="alpha", kind="note", title="Bridge design note",
            summary="shared memory plan for the fleet", tags=["bridge"],
            source={"source_id": "n1"}, created_at=iso(-3600), updated_at=iso(-60),
        ),
        Record(
            bot="alpha", kind="ticket", title="Build bridge",
            summary="freeze the API contract", tags=["bridge"],
            payload={"state": "open", "attention": "Need API contract frozen"},
            source={"source_id": "t1"}, created_at=iso(-10 * 86400), updated_at=iso(-120),
        ),
        Record(
            bot="alpha", kind="ticket", title="Old ticket",
            summary="already done", payload={"state": "ended"},
            source={"source_id": "t2"}, created_at=iso(-11 * 86400), updated_at=iso(-900),
        ),
        Record(
            bot="beta", kind="note", title="Beta note about bridge",
            summary="other bot observation", source={"source_id": "n2"},
            created_at=iso(-7200), updated_at=iso(-30),
        ),
        Record(
            bot="beta", kind="decision", title="Accept bridge plan",
            summary="agreed", source={"source_id": "d1"},
            created_at=iso(-9 * 86400), updated_at=iso(-600),
        ),
    ]
    hub.write_records(records)
    card = Board(hub).add_card(title="First card", desc="seed", assignee="alpha", tags=["bridge"])
    today = datetime.now(timezone.utc).date().isoformat()
    hub.write_digest("alpha", today, "# Alpha digest\n\n- seeded\n")
    hub.write_redaction_stats({"total": 3, "by_rule": {"api_key": 1, "internal_host": 2}})
    hub.write_manifest({"last_sync": records[0].updated_at})
    return {
        "records": records,
        "ids": {record.source.get("source_id"): record.id for record in records},
        "card": card,
        "today": today,
        "last_sync": records[0].updated_at,
    }


@pytest.fixture()
def api(config, hub, web_dir, dataset):
    with served(config, hub, web_dir) as server:
        yield server


# ---------------------------------------------------------------------------------------------
# health / overview / bots
# ---------------------------------------------------------------------------------------------
def test_health(api, dataset, hub):
    status, body, headers = request(api, "GET", "/api/health")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert body["ok"] is True
    assert body["version"]
    assert isinstance(body["uptime_s"], int) and body["uptime_s"] >= 0
    assert body["hub"]["dir"] == str(hub.root)
    assert body["hub"]["records"] == len(dataset["records"])
    assert body["hub"]["bots"] == 2
    assert body["hub"]["last_sync"] == dataset["last_sync"]
    assert body["redaction"] == {"total": 3, "by_rule": {"api_key": 1, "internal_host": 2}}


def test_overview(api, dataset):
    status, body, _ = request(api, "GET", "/api/overview")
    assert status == 200
    assert set(body) == {"generated_at", "bots", "counts", "activity", "recent", "attention"}
    assert body["generated_at"]

    assert len(body["bots"]) == 2
    alpha = next(bot for bot in body["bots"] if bot["id"] == "alpha")
    assert set(alpha) == {
        "id", "label", "profile", "color", "emoji", "status", "last_sync",
        "counts", "open_tickets", "attention",
    }
    assert alpha["label"] == "Alpha"
    assert alpha["color"].startswith("#")
    assert alpha["emoji"]
    assert alpha["status"] in {"active", "idle", "stale", "unknown"}
    assert alpha["counts"] == {"note": 1, "ticket": 2}
    assert alpha["open_tickets"] == 1
    assert alpha["attention"] == 1

    assert body["counts"]["records"] == 5
    assert body["counts"]["by_kind"] == {"decision": 1, "note": 2, "ticket": 2}
    assert body["counts"]["attention"] == 1

    assert len(body["activity"]) == 14
    assert all(set(day) == {"date", "count"} for day in body["activity"])
    assert sum(day["count"] for day in body["activity"]) == len(dataset["records"])

    assert len(body["recent"]) == 5
    brief = body["recent"][0]
    assert set(brief) == {
        "id", "bot", "kind", "title", "summary", "created_at", "updated_at", "tags",
    }
    assert brief["id"] == dataset["ids"]["n2"]  # newest updated_at

    assert len(body["attention"]) == 1
    attention = body["attention"][0]
    assert set(attention) == {"bot", "record_id", "title", "question", "at"}
    assert attention["bot"] == "alpha"
    assert attention["record_id"] == dataset["ids"]["t1"]
    assert attention["question"] == "Need API contract frozen"


def test_bots(api, dataset):
    status, body, _ = request(api, "GET", "/api/bots")
    assert status == 200
    assert [bot["id"] for bot in body["items"]] == ["alpha", "beta"]

    status, body, _ = request(api, "GET", "/api/bots/alpha")
    assert status == 200
    assert body["bot"]["id"] == "alpha"
    assert body["bot"]["label"] == "Alpha"
    assert len(body["recent"]) == 3
    assert all(item["bot"] == "alpha" for item in body["recent"])
    assert body["digests"] == [
        {"date": dataset["today"], "path": f"digests/alpha/{dataset['today']}.md"}
    ]

    status, body, _ = request(api, "GET", "/api/bots/nope")
    assert status == 404
    assert body["error"]["code"] == "not_found"


def test_digest(api, dataset):
    status, body, _ = request(api, "GET", f"/api/digests/alpha/{dataset['today']}")
    assert status == 200
    assert body["bot"] == "alpha"
    assert body["date"] == dataset["today"]
    assert body["content"].startswith("# Alpha digest")

    status, body, _ = request(api, "GET", "/api/digests/alpha/2020-01-01")
    assert status == 404
    assert body["error"]["code"] == "not_found"

    status, body, _ = request(api, "GET", "/api/digests/alpha/not-a-date")
    assert status == 400
    assert body["error"]["code"] == "bad_request"

    status, body, _ = request(api, "GET", f"/api/digests/nope/{dataset['today']}")
    assert status == 404


# ---------------------------------------------------------------------------------------------
# records / search
# ---------------------------------------------------------------------------------------------
def test_records_list_filters_pagination(api, dataset):
    status, body, _ = request(api, "GET", "/api/records")
    assert status == 200
    assert body["total"] == 5
    assert body["limit"] == 50 and body["offset"] == 0
    assert len(body["items"]) == 5
    assert {item["id"] for item in body["items"]} == set(dataset["ids"].values())
    # Newest-updated first.
    assert body["items"][0]["id"] == dataset["ids"]["n2"]

    status, body, _ = request(api, "GET", "/api/records?bot=alpha")
    assert body["total"] == 3
    assert all(item["bot"] == "alpha" for item in body["items"])

    status, body, _ = request(api, "GET", "/api/records?bot=all&kind=note")
    assert body["total"] == 2
    assert all(item["kind"] == "note" for item in body["items"])

    status, body, _ = request(api, "GET", "/api/records?q=bridge")
    assert body["total"] == 4

    status, body, _ = request(api, "GET", "/api/records?limit=2&offset=1")
    assert body["total"] == 5
    assert body["limit"] == 2 and body["offset"] == 1
    assert len(body["items"]) == 2

    status, body, _ = request(api, "GET", "/api/records?limit=2&offset=99")
    assert status == 200 and body["items"] == [] and body["total"] == 5

    status, body, _ = request(api, "GET", "/api/records?limit=abc")
    assert status == 400
    assert body["error"]["code"] == "bad_request"


def test_record_detail(api, dataset):
    record_id = dataset["ids"]["t1"]
    encoded = urllib.request.quote(record_id, safe="")
    status, body, _ = request(api, "GET", f"/api/records/{encoded}")
    assert status == 200
    assert body["id"] == record_id
    assert body["v"] == 1
    assert body["bot"] == "alpha" and body["kind"] == "ticket"
    assert body["payload"]["state"] == "open"
    assert set(body) >= {
        "v", "id", "bot", "kind", "title", "summary", "created_at",
        "updated_at", "tags", "refs", "source", "payload", "redactions",
    }

    status, body, _ = request(api, "GET", f"/api/records/{urllib.request.quote('alpha:note:nope', safe='')}")
    assert status == 404
    assert body["error"]["code"] == "not_found"


def test_search(api):
    status, body, _ = request(api, "GET", "/api/search?q=bridge")
    assert status == 200
    assert body["query"] == "bridge"
    assert len(body["items"]) >= 3
    item = body["items"][0]
    assert set(item) == {"record", "score", "snippet"}
    assert set(item["record"]) == {
        "id", "bot", "kind", "title", "summary", "created_at", "updated_at", "tags",
    }
    assert isinstance(item["score"], float)
    assert isinstance(item["snippet"], str)
    assert all("bridge" in json.dumps(hit["record"]).casefold() or "bridge" in hit["snippet"].casefold()
               for hit in body["items"])

    status, body, _ = request(api, "GET", "/api/search?q=bridge&bot=beta")
    assert status == 200
    assert body["items"] and all(hit["record"]["bot"] == "beta" for hit in body["items"])

    status, body, _ = request(api, "GET", "/api/search")
    assert status == 400
    assert body["error"]["code"] == "bad_request"


# ---------------------------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------------------------
def test_board_read_and_crud(api, dataset):
    status, body, _ = request(api, "GET", "/api/board")
    assert status == 200
    assert [column["id"] for column in body["columns"]] == [
        "inbox", "in_progress", "review", "done",
    ]
    assert len(body["cards"]) == 1
    assert body["cards"][0]["id"] == dataset["card"].id
    assert set(body["cards"][0]) == {
        "id", "title", "desc", "column", "assignee", "tags",
        "created_at", "updated_at", "author",
    }

    status, body, _ = request(
        api, "POST", "/api/board/cards",
        body={"title": "New card", "desc": "created from the API", "column": "in_progress",
              "assignee": "beta", "tags": ["bridge"], "author": "human"},
    )
    assert status == 201
    created = body["card"]
    assert created["title"] == "New card"
    assert created["column"] == "in_progress"
    assert created["assignee"] == "beta"
    assert created["tags"] == ["bridge"]
    card_id = created["id"]

    status, body, _ = request(
        api, "PATCH", f"/api/board/cards/{card_id}",
        body={"title": "Renamed card", "column": "review", "assignee": "alpha", "tags": ["infra"]},
    )
    assert status == 200
    assert body["card"]["title"] == "Renamed card"
    assert body["card"]["column"] == "review"
    assert body["card"]["assignee"] == "alpha"
    assert body["card"]["tags"] == ["infra"]

    status, body, _ = request(api, "GET", "/api/board")
    assert {c["id"] for c in body["cards"]} == {dataset["card"].id, card_id}

    status, body, _ = request(api, "DELETE", f"/api/board/cards/{card_id}")
    assert status == 200 and body == {"ok": True}

    status, body, _ = request(api, "DELETE", f"/api/board/cards/{card_id}")
    assert status == 404


def test_board_validation(api, dataset):
    status, body, _ = request(api, "POST", "/api/board/cards", body={"title": "x", "column": "bogus"})
    assert status == 400
    assert body["error"]["code"] == "invalid_column"

    status, body, _ = request(api, "POST", "/api/board/cards", body={"title": "   "})
    assert status == 400

    status, body, _ = request(api, "POST", "/api/board/cards", body={"title": "x", "tags": "nope"})
    assert status == 400

    status, body, _ = request(api, "POST", "/api/board/cards", raw=b"{not json")
    assert status == 400
    assert body["error"]["code"] == "invalid_json"

    status, body, _ = request(api, "PATCH", f"/api/board/cards/{dataset['card'].id}", body={"column": "nope"})
    assert status == 400

    status, body, _ = request(api, "PATCH", "/api/board/cards/c_missing", body={"title": "x"})
    assert status == 404

    status, body, _ = request(api, "PATCH", f"/api/board/cards/{dataset['card'].id}", body={"bogus": 1})
    assert status == 400

    status, body, _ = request(api, "DELETE", "/api/board/cards/c_missing")
    assert status == 404


# ---------------------------------------------------------------------------------------------
# commands / sync
# ---------------------------------------------------------------------------------------------
def test_commands_list_and_skipped_relay(api, hub):
    status, body, _ = request(api, "GET", "/api/commands")
    assert status == 200
    assert body["items"] == []

    # Feishu relay is disabled in the temp config: a real dispatch.relay records status=skipped.
    status, body, _ = request(
        api, "POST", "/api/commands",
        body={"target_bot": "alpha", "text": "check the bridge hub and summarize alpha memory"},
    )
    assert status == 200
    command = body["command"]
    assert command["target_bot"] == "alpha"
    assert command["text"].startswith("check the bridge hub")
    assert command["status"] == "skipped"
    assert set(command) == {
        "id", "target_bot", "text", "via", "status", "message_id", "error", "created_at",
    }

    status, body, _ = request(api, "GET", "/api/commands")
    assert status == 200
    assert body["items"][0]["id"] == command["id"]
    assert body["items"][0]["status"] == "skipped"


def test_commands_relay_states(api, monkeypatch):
    from context_bridge.dispatch import Command as CommandModel

    status, body, _ = request(api, "POST", "/api/commands", body={"target_bot": "nope", "text": "x"})
    assert status == 400 and body["error"]["code"] == "unknown_bot"
    status, body, _ = request(api, "POST", "/api/commands", body={"target_bot": "alpha"})
    assert status == 400
    status, body, _ = request(api, "POST", "/api/commands", body={"target_bot": "alpha", "text": "  "})
    assert status == 400

    def sent_relay(config, hub, target_bot, text):
        return CommandModel(target_bot=target_bot, text=text, status="sent", message_id="om_test")

    monkeypatch.setattr("context_bridge.dispatch.relay", sent_relay)
    status, body, _ = request(api, "POST", "/api/commands", body={"target_bot": "alpha", "text": "do it"})
    assert status == 200
    assert body["command"]["status"] == "sent"
    assert body["command"]["message_id"] == "om_test"

    def failed_relay(config, hub, target_bot, text):
        return CommandModel(target_bot=target_bot, text=text, status="failed", error="lark-cli exploded")

    monkeypatch.setattr("context_bridge.dispatch.relay", failed_relay)
    status, body, _ = request(api, "POST", "/api/commands", body={"target_bot": "beta", "text": "do it"})
    assert status == 502
    assert body["error"]["code"] == "dispatch_failed"
    assert body["command"]["status"] == "failed"

    def raising_relay(config, hub, target_bot, text):
        raise RuntimeError("boom")

    monkeypatch.setattr("context_bridge.dispatch.relay", raising_relay)
    status, body, _ = request(api, "POST", "/api/commands", body={"target_bot": "beta", "text": "do it"})
    assert status == 502
    assert body["error"]["code"] == "dispatch_failed"


def test_sync_trigger_single_flight(api, monkeypatch):
    calls: list[dict] = []
    finished = threading.Event()

    def fake_run_sync(config, bots=None, with_sessions=True, commit=True):
        calls.append({"commit": commit})
        time.sleep(0.4)
        finished.set()
        return {"records": 12, "per_bot": {}, "committed": False, "at": utcnow_iso()}

    monkeypatch.setattr("context_bridge.sync.run_sync", fake_run_sync)

    status, body, _ = request(api, "POST", "/api/sync", body={})
    assert status == 202 and body == {"ok": True}
    status, body, _ = request(api, "POST", "/api/sync", body={})
    assert status == 202 and body == {"ok": True}

    assert finished.wait(3.0), "background sync did not run"
    time.sleep(0.3)  # allow the second (no-op) request path to settle
    assert len(calls) == 1  # single-flight: the concurrent trigger did not start a second pass

    status, body, _ = request(api, "POST", "/api/sync", body={})
    assert status == 202
    assert wait_until(lambda: len(calls) == 2, timeout=3.0)
    time.sleep(0.5)

    status, body, _ = request(api, "POST", "/api/sync", raw=b"{oops")
    assert status == 400


# ---------------------------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------------------------
def test_sse_events(api, hub):
    """The live feed sends hello on subscribe, then sync/board/command events for later writes."""
    port = api.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", "/api/events")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type", "").startswith("text/event-stream")

        # Subscribe first: hello proves the client is registered before we mutate the hub.
        event, data = read_sse_event(response)
        assert event == "hello"
        assert data == {"ok": True}

        # One write per event kind, all strictly after the subscription.
        Board(hub).add_card(title="SSE card")
        (hub.commands_dir / "cmd_sse.json").write_text(
            json.dumps(
                {
                    "id": "cmd_sse", "target_bot": "alpha", "text": "x", "via": "feishu",
                    "status": "sent", "message_id": "om_sse", "error": None,
                    "created_at": utcnow_iso(),
                }
            ),
            encoding="utf-8",
        )
        (hub.state_dir / "last-sync.json").write_text(
            json.dumps({"at": utcnow_iso()}), encoding="utf-8"
        )

        expected = {"board", "command", "sync"}
        seen: dict[str, dict] = {}
        deadline = time.time() + 20
        while time.time() < deadline and not expected <= set(seen):
            event, data = read_sse_event(response)
            seen.setdefault(event, data)
        assert expected <= set(seen), f"missing SSE events: {seen.keys()}"

        assert "at" in seen["board"]
        assert seen["command"]["id"] == "cmd_sse"
        assert isinstance(seen["sync"]["records"], int)
        assert "at" in seen["sync"]
    finally:
        connection.close()


# ---------------------------------------------------------------------------------------------
# static content / routing / errors
# ---------------------------------------------------------------------------------------------
def test_static_spa_and_traversal(api):
    status, body, headers = request(api, "GET", "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"CONSOLE_MARKER" in body

    status, body, headers = request(api, "GET", "/app.js")
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")
    assert b"console.log" in body

    status, body, headers = request(api, "GET", "/styles.css")
    assert status == 200
    assert headers["Content-Type"].startswith("text/css")

    status, body, _ = request(api, "GET", "/bots/alpha")
    assert status == 200
    assert b"CONSOLE_MARKER" in body  # SPA fallback

    status, body, _ = request(api, "GET", "/%2e%2e/secret.txt")
    assert status == 403
    assert body["error"]["code"] == "forbidden"
    assert b"TOP-SECRET" not in json.dumps(body).encode()

    status, body, _ = request(api, "GET", "/%2e%2e%2fsecret.txt")
    assert status == 403

    status, body, _ = request(api, "GET", "/api/nope")
    assert status == 404
    assert body["error"]["code"] == "not_found"

    status, body, _ = request(api, "PUT", "/api/health")
    assert status == 405
    assert body["error"]["code"] == "method_not_allowed"


def test_placeholder_when_web_missing(config, hub, tmp_path):
    empty_web = tmp_path / "no-web"
    empty_web.mkdir()
    with served(config, hub, empty_web) as server:
        status, body, headers = request(server, "GET", "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert b"not installed" in body

        status, body, _ = request(server, "GET", "/some/spa/route")
        assert status == 200
        assert b"not installed" in body


def test_bind_any_host_and_ephemeral_port(config, hub, web_dir):
    """Deployment binds 0.0.0.0 with a configured port; port 0 must yield an ephemeral one."""
    config.server.host = "0.0.0.0"
    config.server.port = 0
    server = create_server(config, hub=hub, web_dir=web_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_address[1] != 0
        status, body, _ = request(server, "GET", "/api/health")
        assert status == 200 and body["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_run_in_thread_helper(config, hub, web_dir):
    config.server.port = 0
    server = run_in_thread(config, hub=hub, web_dir=web_dir)
    try:
        thread = getattr(server, "http_thread", None)
        assert thread is not None and thread.is_alive()
        status, body, _ = request(server, "GET", "/api/health")
        assert status == 200 and body["ok"] is True
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------------------------
# optional bearer token
# ---------------------------------------------------------------------------------------------
def test_optional_bearer_token(config, hub, web_dir, dataset):
    with served(config, hub, web_dir, token="s3cret") as server:
        status, body, headers = request(server, "GET", "/api/health")
        assert status == 401
        assert body["error"]["code"] == "unauthorized"
        assert headers["WWW-Authenticate"] == "Bearer"

        status, body, _ = request(server, "GET", "/api/health", token="wrong")
        assert status == 401

        status, body, _ = request(server, "GET", "/api/health", token="s3cret")
        assert status == 200

        status, body, headers = request(server, "GET", "/api/health?token=s3cret")
        assert status == 200
        cookie_header = headers.get("Set-Cookie", "")
        assert cookie_header.startswith("ctxbridge_token=")
        cookie = cookie_header.split(";", 1)[0]

        status, body, _ = request(server, "GET", "/")
        assert status == 401  # HTML documents are protected too

        status, body, headers = request(server, "GET", "/?token=s3cret")
        assert status == 200
        assert b"CONSOLE_MARKER" in body

        status, body, headers = request(server, "GET", "/app.js")
        assert status == 200  # static assets stay reachable so the console can boot

        status, body, _ = request(server, "GET", "/api/health", headers={"Cookie": cookie})
        assert status == 200
