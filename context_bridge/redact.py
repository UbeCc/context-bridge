"""Secret and internal-endpoint redaction.

Everything written to the hub passes through :class:`Redactor`. The rules are intentionally
conservative about *identifiers* (git SHAs, UUIDs and Feishu ids are kept — they are useful and
not credentials) and aggressive about *secrets* and *internal endpoints*.

Contract (implemented by the data-layer owner):

- ``Redactor(config).clean(text) -> RedactionResult(text, count, rules)``
- ``Redactor.clean_obj(value) -> (value, count)`` recursively cleans strings in dict/list
- ``Redactor.stats() -> dict`` per-rule counters for ``state/redaction.json``
- patterns are compiled once; no network, no external dependencies

Design notes:

- Rules are applied in a fixed order. Shape-based secret rules run before the generic
  high-entropy rule so that e.g. an ``sk-...`` key is counted as ``openai_key`` rather than as an
  anonymous blob.
- The replacement mask (``[REDACTED]``) never matches a rule again, so cleaning already-cleaned
  text is idempotent and does not inflate the counters.
- The high-entropy rule deliberately ignores pure hex blobs: 40-char git SHAs, 32-char ticket /
  trigger ids, 64-char content hashes and UUIDs are identifiers, not credentials. A pure-hex
  secret is still caught when it appears next to a secret-ish key name (``token: <hex>``).
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

MASK = "[REDACTED]"

LOG = logging.getLogger(__name__)

#: Secret-shaped rules, applied before the generic entropy rule.
_PEM = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    re.DOTALL,
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{15,}")
_GITHUB_TOKEN = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")
_GITHUB_PAT = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")  # public-safety: allow
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[A-Z0-9]{16}\b")
_SLACK_TOKEN = re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")
_SLACK_APP = re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b")
_BEARER = re.compile(r"(?i)\b(Bearer)\s+([A-Za-z0-9._~+/=-]{12,})")
_BASIC_AUTH_URL = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://)([^\s/:@]+):([^\s/@]+)@")

_CRED_KEY = (
    r"(?:password|passwd|pwd|passphrase|secret|client[_-]?secret|api[_-]?key|apikey|"
    r"access[_-]?key|secret[_-]?key|private[_-]?key|auth[_-]?token|access[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|session[_-]?token|token|credentials?)"
)
#: ``password = hunter2``, ``"api_key": "..."``, ``TOKEN=<value>`` and similar assignments.
_CRED_ASSIGN = re.compile(
    r"(?i)(\b" + _CRED_KEY + r"""\b["']?\s*[:=]\s*["']?)(?!\[REDACTED\])([^\s"',;&]{6,})"""
)
#: ``--token abcdef12`` / ``--api-key=abcdef12`` CLI style.
_CLI_SECRET = re.compile(
    r"(?i)(--?(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|"
    r"client[_-]?secret|private[_-]?key)\b[ =:]+)(?!\[REDACTED\])[^\s\"']+"
)

#: Loopback / wildcard endpoints. The URL form is matched first (it may carry a port and path),
#: then bare ``host:port`` mentions that are common in notes and logs.
_LOOPBACK_URL = re.compile(
    r"\b(?:https?|wss?|ftp)://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d{1,5})?"
    r"(?:/[^\s)\"'<>]*)?" 
)
_LOOPBACK_ENDPOINT = re.compile(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):\d{2,5}\b")
_PRIVATE_IP = re.compile(
    r"(?<![\d.])("
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|127\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|169\.254\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"
    r")(?![\d.])"
)
_INTERNAL_HOST = re.compile(
    r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:internal|intranet|local|lan|corp|home\.arpa)\b"
)
#: Absolute user-home paths are host details, not shared memory. Both the plain form and the
#: common ``NO_PROXY=.host`` / trailing-punctuation forms are covered.
_HOME_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:root|home/[A-Za-z0-9_.-]+)(?:/[^\s\"'`)\]}>,;]*)?"
)

#: Candidate for the generic high-entropy secret rule: base64/url-ish tokens with no whitespace.
_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+/_=-]{40,}")


def _shannon(s: str) -> float:
    """Shannon entropy per character (bits)."""
    if not s:
        return 0.0
    counts = Counter(s)
    total = len(s)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _looks_like_secret_blob(token: str) -> bool:
    """True for long random-looking tokens, false for identifiers (SHAs, UUIDs, Feishu ids)."""
    if len(token) < 40:
        return False
    if re.fullmatch(r"[0-9a-fA-F]+", token):  # git SHA-1/SHA-256, ticket/trigger ids
        return False
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", token
    ):  # UUID
        return False
    if re.match(r"(?i)(?:oc|om|ou|cli|app|dsh)[_-]", token):  # Feishu / DS ids carry a prefix
        return False
    # Random secrets mix case and digits; natural-language identifiers rarely do at this length.
    if not (re.search(r"[a-z]", token) and re.search(r"[A-Z]", token) and re.search(r"\d", token)):
        return False
    if token.count("-") + token.count("_") > len(token) // 2:  # hyphenated slug / path fragment
        return False
    return _shannon(token) >= 3.6


def _repl_mask(match: "re.Match[str]") -> str:
    return MASK


def _repl_bearer(match: "re.Match[str]") -> str:
    return f"{match.group(1)} {MASK}"


def _repl_basic_auth(match: "re.Match[str]") -> str:
    return f"{match.group(1)}{MASK}@"


def _repl_cred_assign(match: "re.Match[str]") -> str:
    return f"{match.group(1)}{MASK}"


def _repl_cli_secret(match: "re.Match[str]") -> str:
    return f"{match.group(1)}{MASK}"


def _repl_entropy(match: "re.Match[str]") -> str:
    token = match.group(0)
    # Keep trailing padding "=" out of the entropy sanity check but still mask the whole token.
    return MASK if _looks_like_secret_blob(token.rstrip("=")) else token


@dataclass
class _Rule:
    """One named redaction rule: one or more patterns, a replacement, optional per-match filter."""

    name: str
    patterns: list["re.Pattern[str]"]
    repl: Callable[["re.Match[str]"], str]
    filter: Callable[["re.Match[str]"], bool] | None = None

    def apply(self, text: str, counters: Counter[str]) -> tuple[str, int]:
        hits = 0

        def _sub(match: "re.Match[str]") -> str:
            nonlocal hits
            if self.filter is not None and not self.filter(match):
                return match.group(0)
            replacement = self.repl(match)
            if replacement != match.group(0):
                hits += 1
                counters[self.name] += 1
            return replacement

        for pattern in self.patterns:
            text = pattern.sub(_sub, text)
        return text, hits


def _redaction_config(config: Any) -> Any:
    """Accept a full :class:`Config`, a ``RedactionConfig`` or ``None``."""
    if config is None:
        from .config import RedactionConfig

        return RedactionConfig()
    red = getattr(config, "redaction", None)
    if red is not None:
        return red
    if hasattr(config, "mask_internal_domains") or hasattr(config, "enabled"):
        return config
    from .config import RedactionConfig

    return RedactionConfig()


@dataclass
class RedactionResult:
    """One cleaned string plus what was masked."""

    text: str
    count: int = 0
    rules: list[str] = field(default_factory=list)


class Redactor:
    """Mask secrets and internal endpoints in every string that reaches the hub."""

    MASK = MASK

    def __init__(self, config: Any = None) -> None:
        red = _redaction_config(config)
        self.config = config
        self.enabled = bool(getattr(red, "enabled", True))
        self.mask_private_ips = bool(getattr(red, "mask_private_ips", True))
        domains = [str(d).strip().lstrip(".") for d in (getattr(red, "mask_internal_domains", None) or [])]
        self.internal_domains: list[str] = [d for d in domains if d]
        self.extra_patterns: list[str] = [str(p) for p in (getattr(red, "extra_patterns", None) or [])]
        self._counters: Counter[str] = Counter()
        self._rules = self._compile_rules()

    # -- rule construction ---------------------------------------------------------
    def _compile_rules(self) -> list[_Rule]:
        rules: list[_Rule] = [
            _Rule("pem_private_key", [_PEM], _repl_mask),
            _Rule("jwt", [_JWT], _repl_mask),
            _Rule("basic_auth_url", [_BASIC_AUTH_URL], _repl_basic_auth),
            _Rule("bearer_token", [_BEARER], _repl_bearer),
            _Rule("openai_key", [_OPENAI_KEY], _repl_mask),
            _Rule("github_token", [_GITHUB_TOKEN, _GITHUB_PAT], _repl_mask),
            _Rule("aws_access_key", [_AWS_KEY], _repl_mask),
            _Rule("slack_token", [_SLACK_TOKEN, _SLACK_APP], _repl_mask),
            _Rule("credential_assignment", [_CRED_ASSIGN], _repl_cred_assign),
            _Rule("cli_secret_argument", [_CLI_SECRET], _repl_cli_secret),
            _Rule("loopback_url", [_LOOPBACK_URL, _LOOPBACK_ENDPOINT], _repl_mask),
        ]
        if self.mask_private_ips:
            rules.append(_Rule("private_ip", [_PRIVATE_IP], _repl_mask))
        for domain in self.internal_domains:
            # Mask the configured domain, its sub-domains and dot-prefixed forms (".host" shows
            # up in NO_PROXY lists and shell env dumps). Only word characters/hyphens block a
            # match: a leading "." is part of the endpoint, not a boundary.
            pattern = re.compile(
                r"(?i)(?<![A-Za-z0-9-])\.?(?:[\w-]+\.)*" + re.escape(domain) + r"(?![\w-])"
            )
            rules.append(_Rule("internal_domain", [pattern], _repl_mask))
        rules.append(_Rule("internal_host", [_INTERNAL_HOST], _repl_mask))
        rules.append(_Rule("home_path", [_HOME_PATH], _repl_mask))
        for raw in self.extra_patterns:
            try:
                pattern = re.compile(raw)
            except re.error as exc:
                # A malformed pattern is a config bug, not a reason to mask random characters:
                # skip it loudly and keep the built-in rules working.
                LOG.warning("redact: ignoring invalid redaction.extra_patterns entry %r: %s", raw, exc)
                continue
            rules.append(_Rule("extra_pattern", [pattern], _repl_mask))
        rules.append(
            _Rule("high_entropy_secret", [_ENTROPY_CANDIDATE], _repl_entropy, filter=_entropy_filter)
        )
        return rules

    # -- public API ----------------------------------------------------------------
    def clean(self, text: str) -> RedactionResult:
        """Mask every rule match in ``text`` and report what was masked."""
        if not isinstance(text, str):
            text = str(text)
        if not self.enabled:
            return RedactionResult(text=text)
        rules_hit: list[str] = []
        count = 0
        for rule in self._rules:
            text, hits = rule.apply(text, self._counters)
            if hits:
                count += hits
                rules_hit.extend([rule.name] * hits)
        return RedactionResult(text=text, count=count, rules=rules_hit)

    def clean_obj(self, value: Any) -> tuple[Any, int]:
        """Recursively clean strings inside dicts/lists. Returns ``(new_value, count)``."""
        if isinstance(value, str):
            result = self.clean(value)
            return result.text, result.count
        if isinstance(value, dict):
            cleaned: dict[Any, Any] = {}
            total = 0
            for key, item in value.items():
                cleaned[key], n = self.clean_obj(item)
                total += n
            return cleaned, total
        if isinstance(value, (list, tuple)):
            items = []
            total = 0
            for item in value:
                clean_item, n = self.clean_obj(item)
                items.append(clean_item)
                total += n
            return items, total
        return value, 0

    def stats(self) -> dict[str, Any]:
        """Per-rule counters accumulated since construction (for ``state/redaction.json``)."""
        by_rule = {name: int(n) for name, n in self._counters.items() if n}
        return {"total": int(sum(by_rule.values())), "by_rule": by_rule}


def _entropy_filter(match: "re.Match[str]") -> bool:
    return _looks_like_secret_blob(match.group(0).rstrip("="))


def clean(config: Any, text: str) -> RedactionResult:
    """Convenience one-shot helper; collectors should share one :class:`Redactor` for stats."""
    return Redactor(config).clean(text)


__all__ = ["MASK", "RedactionResult", "Redactor", "clean"]
