"""Relay a person's instruction to a bot through its chat source.

The console does not inject into another process. It sends a chat message that *addresses* the
target bot (a real mention in Feishu), so the bot's own intake, authorization and audit rules
still apply. The bridge records the command in ``commands/<id>.json`` for traceability.

    relay(config, hub, target_bot, text) -> Command
    relay_enabled(config) -> bool

The message is sent with ``lark-cli im +messages-send`` as the configured bridge bot: the
relay chat id, the identity wrapper (``feishu.cli``) and each bot's ``open_id`` all come from the
deployment config. The mention body uses Feishu's text at-tag
(``<at user_id="ou_...">Label</at>``); browsers without a configured ``open_id`` fall back to a
plain ``@Label`` so the bridge never silently drops the addressee.

Testability: pass ``runner=<callable(argv) -> CompletedProcess>`` to avoid invoking the CLI, and
set ``CTXBRIDGE_DISPATCH_DRY_RUN=1`` to simulate a successful send without any external process.
A relay that is disabled in config is still recorded, with ``status="skipped"``.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Callable, Sequence

from .model import Command, Record, utcnow_iso
from .redact import Redactor

DRY_RUN_ENV = "CTXBRIDGE_DISPATCH_DRY_RUN"
RUNNER_TIMEOUT_S = 60.0

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def relay_enabled(config) -> bool:
    """True when the chat relay is configured well enough to attempt a send."""
    feishu = getattr(config, "feishu", None)
    if feishu is None:
        return False
    return bool(getattr(feishu, "enabled", False) and getattr(feishu, "relay_chat_id", ""))


def _dry_run() -> bool:
    return os.environ.get(DRY_RUN_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _redact_text(config, text: str) -> tuple[str, int]:
    if config is not None and not getattr(getattr(config, "redaction", None), "enabled", True):
        return text, 0
    result = Redactor(config).clean(text)
    return result.text, int(getattr(result, "count", 0) or 0)


def _clean(text: str, limit: int = 400) -> str:
    return " ".join(str(text or "").split())[:limit]


def build_mention(config, target_bot: str) -> str:
    """The at-tag that addresses ``target_bot`` in the relay chat."""
    spec = getattr(config, "bots", {}).get(target_bot)
    label = getattr(spec, "label", None) or target_bot
    targets = getattr(getattr(config, "feishu", None), "targets", None) or {}
    target = targets.get(target_bot) or {}
    open_id = str(target.get("open_id") or target.get("user_id") or "").strip()
    if open_id:
        return f'<at user_id="{open_id}">{label}</at>'
    return f"@{label}"


def build_send_argv(
    config, target_bot: str, text: str, idempotency_key: str | None = None
) -> list[str]:
    """The exact ``lark-cli`` argv for one relayed instruction (kept pure/testable)."""
    mention = build_mention(config, target_bot)
    body = f"{mention} {text}".strip() if mention else text
    content = json.dumps({"text": body}, ensure_ascii=False, separators=(",", ":"))
    feishu = config.feishu
    argv = [
        str(feishu.cli or "lark-cli"),
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--chat-id",
        str(feishu.relay_chat_id),
        "--msg-type",
        "text",
        "--content",
        content,
    ]
    if idempotency_key:
        argv += ["--idempotency-key", str(idempotency_key)[:50]]
    return argv


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=RUNNER_TIMEOUT_S
    )


def _json_error(payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return _clean(error.get("message") or error.get("msg") or json.dumps(error, ensure_ascii=False))
    if error:
        return _clean(error)
    return _clean(payload.get("msg") or payload.get("message") or "")


def _parse_result(proc: subprocess.CompletedProcess) -> tuple[bool, str | None, str | None]:
    """Return ``(ok, message_id, error)`` from a lark-cli process result."""
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    payload = None
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            start, end = stdout.find("{"), stdout.rfind("}")
            if 0 <= start < end:
                try:
                    payload = json.loads(stdout[start : end + 1])
                except json.JSONDecodeError:
                    payload = None
    if proc.returncode != 0:
        detail = _json_error(payload) if isinstance(payload, dict) else ""
        detail = detail or _clean(stderr) or _clean(stdout) or f"lark-cli exited {proc.returncode}"
        return False, None, detail
    if isinstance(payload, dict) and payload.get("ok") is False:
        return False, None, _json_error(payload) or "lark-cli reported a failure"
    message_id = None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            message_id = data.get("message_id") or data.get("messageId")
            nested = data.get("data")
            if not message_id and isinstance(nested, dict):
                message_id = nested.get("message_id") or nested.get("messageId")
        if not message_id:
            message_id = payload.get("message_id") or payload.get("messageId")
    if not message_id:
        return False, None, "lark-cli returned no message_id (unexpected output format)"
    return True, str(message_id), None


def relay(
    config,
    hub,
    target_bot: str,
    text: str,
    *,
    runner: Runner | None = None,
) -> Command:
    """Send ``text`` to ``target_bot`` through the configured chat source and record it.

    Never raises for a delivery failure: the returned :class:`Command` carries
    ``status="failed"`` and a redacted ``error`` instead. ``ValueError`` is raised only for an
    invalid target bot or empty text, before anything is sent.
    """
    bots = getattr(config, "bots", None) or {}
    if target_bot not in bots:
        raise ValueError(
            f"unknown bot {target_bot!r}; configured: {', '.join(sorted(bots)) or '(none)'}"
        )
    text = (text or "").strip()
    if not text:
        raise ValueError("command text must not be empty")
    if len(text) > 8000:
        text = text[:8000]

    stored_text, redactions = _redact_text(config, text)
    command = Command(target_bot=target_bot, text=stored_text)

    if not relay_enabled(config):
        command.status = "skipped"
        command.error = "chat relay is disabled (feishu.enabled=false or relay_chat_id missing)"
        _record(hub, command, redactions)
        return command

    argv = build_send_argv(config, target_bot, text, idempotency_key=command.id)

    if _dry_run():
        # Simulated success: validates the recorded command path without any network/CLI use.
        command.status = "sent"
        command.message_id = "dry-run"
        _record(hub, command, redactions)
        return command

    execute = runner or _default_runner
    try:
        proc = execute(argv)
    except subprocess.TimeoutExpired:
        command.status = "failed"
        command.error = f"lark-cli timed out after {RUNNER_TIMEOUT_S:.0f}s"
    except (OSError, ValueError) as exc:
        command.status = "failed"
        command.error = _clean(str(exc)) or "could not execute the chat CLI"
    else:
        ok, message_id, error = _parse_result(proc)
        if ok:
            command.status = "sent"
            command.message_id = message_id
        else:
            command.status = "failed"
            command.error = _clean(error or "chat relay failed")

    if command.error:
        # The error text may echo message content; redact before it reaches the hub.
        command.error, _ = _redact_text(config, command.error)
    _record(hub, command, redactions)
    return command


def _record(hub, command: Command, redactions: int = 0) -> None:
    """Persist the command file and a searchable ``command`` record, then commit."""
    hub.write_command(command)
    body = " ".join(command.text.split())
    record = Record(
        bot=command.target_bot,
        kind="command",
        title=(body[:120] or f"command to {command.target_bot}"),
        summary=command.text,
        tags=["command", command.status],
        created_at=command.created_at,
        updated_at=utcnow_iso(),
        source={"system": "ctxbridge", "source_id": command.id},
        payload={
            "command_id": command.id,
            "target_bot": command.target_bot,
            "via": command.via,
            "status": command.status,
            "message_id": command.message_id,
            "error": command.error,
        },
        redactions=redactions,
    )
    hub.write_records([record])
    hub.commit(f"command: {command.status} {command.id} -> {command.target_bot}")
