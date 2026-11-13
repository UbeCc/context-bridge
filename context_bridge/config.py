"""Deployment configuration: ``config.toml`` parsing and validation.

The config is deliberately small and declarative. It never contains credentials: chat relay
credentials come from the environment or from the CLI tool's own login state.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import BotSpec, BOARD_COLUMN_IDS


class ConfigError(ValueError):
    """Raised when a deployment config is missing or inconsistent."""


@dataclass
class HubConfig:
    dir: str = ""
    remote: str = ""
    commit: bool = True
    auto_push: bool = False


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    token: str = ""  # optional bearer token for the API/console


@dataclass
class RedactionConfig:
    enabled: bool = True
    mask_internal_domains: list[str] = field(default_factory=lambda: ["corp.example.com"])
    mask_private_ips: bool = True
    extra_patterns: list[str] = field(default_factory=list)


@dataclass
class FeishuConfig:
    enabled: bool = False
    cli: str = "lark-cli"
    identity: str = "opus"
    relay_chat_id: str = ""
    targets: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class Config:
    hub: HubConfig
    server: ServerConfig = field(default_factory=ServerConfig)
    redaction: RedactionConfig = field(default_factory=RedactionConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    bots: dict[str, BotSpec] = field(default_factory=dict)
    path: Path | None = None

    def bot(self, bot_id: str) -> BotSpec:
        try:
            return self.bots[bot_id]
        except KeyError:
            raise ConfigError(f"unknown bot {bot_id!r}; configured: {', '.join(sorted(self.bots)) or '(none)'}") from None

    def validate(self) -> None:
        if not self.hub.dir:
            raise ConfigError("hub.dir is required")
        if not self.bots:
            raise ConfigError("at least one [bots.<id>] section is required")
        for bot_id, spec in self.bots.items():
            if not spec.state_dir:
                raise ConfigError(f"bots.{bot_id}.state_dir is required")
        if not (0 < int(self.server.port) < 65536):
            raise ConfigError("server.port must be between 1 and 65535")
        if self.feishu.enabled and not self.feishu.relay_chat_id:
            raise ConfigError("feishu.enabled requires feishu.relay_chat_id")
        unknown = [c for c in (self.feishu.targets or {}) if c not in self.bots]
        if unknown:
            raise ConfigError(f"feishu.targets references unknown bots: {', '.join(unknown)}")


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def parse_config(data: dict[str, Any], path: Path | None = None) -> Config:
    hub = HubConfig(**_pick(data, "hub", HubConfig))
    server = ServerConfig(**_pick(data, "server", ServerConfig))
    redaction = RedactionConfig(**_pick(data, "redaction", RedactionConfig))
    feishu = FeishuConfig(**_pick(data, "feishu", FeishuConfig))

    bots: dict[str, BotSpec] = {}
    for bot_id, raw in _section(data, "bots").items():
        if not isinstance(raw, dict):
            raise ConfigError(f"bots.{bot_id} must be a table")
        spec = dict(raw)
        spec.setdefault("id", bot_id)
        spec.setdefault("label", bot_id.capitalize())
        spec.setdefault("profile", bot_id)
        spec.setdefault("state_dir", "")
        # Accept only known keys so typos fail loudly.
        allowed = {"id", "label", "profile", "state_dir", "sessions_root", "color", "emoji"}
        unknown = set(spec) - allowed
        if unknown:
            raise ConfigError(f"bots.{bot_id}: unknown keys {sorted(unknown)}")
        bots[bot_id] = BotSpec(**spec)

    if feishu.targets is None:
        feishu.targets = {}
    config = Config(hub=hub, server=server, redaction=redaction, feishu=feishu, bots=bots, path=path)
    # Paths may be relative to the config file's directory; make them absolute for daemons.
    if config.path is not None:
        base = config.path.parent
        config.hub.dir = str(_abs(config.hub.dir, base))
        for spec in config.bots.values():
            spec.state_dir = str(_abs(spec.state_dir, base))
            if spec.sessions_root:
                spec.sessions_root = str(_abs(spec.sessions_root, base))
    config.validate()
    return config


def _pick(data: dict[str, Any], section: str, cls: type) -> dict[str, Any]:
    raw = _section(data, section)
    allowed = set(getattr(cls, "__dataclass_fields__", {}))
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"[{section}]: unknown keys {sorted(unknown)}")
    return dict(raw)


def _abs(value: str, base: Path) -> Path:
    if not value:
        return Path(value)
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    return expanded if expanded.is_absolute() else (base / expanded)


def load_config(path: str | Path) -> Config:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top level must be a table")
    return parse_config(data, path=p)


def config_template(hub_dir: str = "./hub", port: int = 8080) -> str:
    """A commented starter config, printed by ``ctxbridge gen-config``."""
    return f"""# Context Bridge deployment config

[hub]
dir = "{hub_dir}"
# remote = "git@github.com:org/private-context-hub.git"   # optional, private data only
commit = true
auto_push = false

[server]
host = "127.0.0.1"
port = {port}
# token = "set-a-token-to-require-bearer-auth"

[redaction]
enabled = true
mask_internal_domains = ["corp.example.com"]
mask_private_ips = true

# [feishu]
# enabled = true
# cli = "lark-cli"
# identity = "opus"
# relay_chat_id = "oc_xxx"
# [feishu.targets.sonnet]
# open_id = "ou_xxx"
# [feishu.targets.fable]
# open_id = "ou_xxx"

[bots.opus]
label = "Opus"
profile = "superagent"
state_dir = "~/bots/opus/state"
sessions_root = "~/.dsh/sessions"
color = "#7c5cff"
emoji = "🟣"

[bots.sonnet]
label = "Sonnet"
profile = "superagent-sonnet"
state_dir = "~/bots/sonnet/state"
sessions_root = "~/.dsh/sessions"
color = "#2fc8b5"
emoji = "🩵"

[bots.fable]
label = "Fable"
profile = "superagent-fable"
state_dir = "~/bots/fable/state"
sessions_root = "~/.dsh/sessions"
color = "#f0a13b"
emoji = "🟠"
"""
