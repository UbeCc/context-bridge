"""Relay a person's instruction to a bot through its chat source.

The console does not inject into another process. It sends a chat message that *addresses* the
target bot (a real mention in Feishu), so the bot's own intake, authorization and audit rules
still apply. The bridge records the command in ``commands/<id>.json`` for traceability.

Contract (implemented by the hub/CLI owner, live-verified by the integrator/tester):

    relay(config, hub, target_bot, text) -> Command
    relay_enabled(config) -> bool
"""

from __future__ import annotations

from .model import Command


def relay_enabled(config) -> bool:
    raise NotImplementedError("dispatch.relay_enabled is implemented by the hub/CLI owner")


def relay(config, hub, target_bot: str, text: str) -> Command:
    raise NotImplementedError("dispatch.relay is implemented by the hub/CLI owner")
