"""Secret and internal-endpoint redaction.

Everything written to the hub passes through :class:`Redactor`. The rules are intentionally
conservative about *identifiers* (git SHAs, UUIDs and Feishu ids are kept — they are useful and
not credentials) and aggressive about *secrets* and *internal endpoints*.

Contract (implemented by the data-layer owner):

- ``Redactor(config).clean(text) -> RedactionResult(text, count, rules)``
- ``Redactor.clean_obj(value) -> (value, count)`` recursively cleans strings in dict/list
- ``Redactor.stats() -> dict`` per-rule counters for ``state/redaction.json``
- patterns are compiled once; no network, no external dependencies
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RedactionResult:
    """One cleaned string plus what was masked."""

    text: str
    count: int = 0
    rules: list[str] = field(default_factory=list)


class Redactor:
    """Mask secrets and internal endpoints in every string that reaches the hub."""

    MASK = "[REDACTED]"

    def __init__(self, config=None) -> None:
        self.config = config
        # TODO(data-layer): compile built-in patterns + config.redaction.extra_patterns.

    def clean(self, text: str) -> RedactionResult:
        raise NotImplementedError("redact.clean is implemented by the data-layer owner")

    def clean_obj(self, value):
        raise NotImplementedError("redact.clean_obj is implemented by the data-layer owner")

    def stats(self) -> dict:
        raise NotImplementedError("redact.stats is implemented by the data-layer owner")
