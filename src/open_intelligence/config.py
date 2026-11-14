"""Settings: built-in source definitions, optionally overridden by a TOML file.

Lookup order for the file: the ``--config`` argument, then ``$OPEN_INTELLIGENCE_CONFIG``, then
``./open-intelligence.toml``, then ``~/.config/open-intelligence/config.toml``. No file is fine: the
built-in definitions connect to the three official remote servers.

Tokens are never read from the file. A source names the environment variables that hold its token
(``token_env``); the file may change those names but cannot contain a token or an Authorization header.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from open_intelligence.sources import BUILTIN_SOURCES, SourceSpec

CONFIG_ENV = "OPEN_INTELLIGENCE_CONFIG"

_SOURCE_KEYS = {
    "enabled",
    "transport",
    "url",
    "headers",
    "command",
    "args",
    "env",
    "token_env",
    "token_required",
    "allowed_tools",
}
_GATEWAY_KEYS = {"timeout_seconds", "max_result_chars"}
_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey")
_SECRET_HEADERS = {"authorization", "cookie", "x-api-key"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    sources: dict[str, SourceSpec]
    timeout_s: float = 45.0
    max_result_chars: int = 12_000
    config_path: Path | None = None


def default_paths(environ: Mapping[str, str] = os.environ) -> list[Path]:
    paths = []
    if environ.get(CONFIG_ENV):
        paths.append(Path(environ[CONFIG_ENV]).expanduser())
    paths.append(Path.cwd() / "open-intelligence.toml")
    paths.append(Path.home() / ".config" / "open-intelligence" / "config.toml")
    return paths


def load_settings(path: str | Path | None = None, environ: Mapping[str, str] = os.environ) -> Settings:
    chosen: Path | None
    if path is not None:
        explicit = Path(path).expanduser()
        if not explicit.is_file():
            raise ConfigError(f"config file not found: {explicit}")
        chosen = explicit
    else:
        chosen = next((p for p in default_paths(environ) if p.is_file()), None)
    if chosen is None:
        return Settings(sources=dict(BUILTIN_SOURCES))
    try:
        data = tomllib.loads(chosen.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{chosen}: {exc}") from exc
    return settings_from_mapping(data, config_path=chosen)


def settings_from_mapping(data: Mapping[str, Any], config_path: Path | None = None) -> Settings:
    unknown_top = set(data) - {"gateway", "sources"}
    if unknown_top:
        raise ConfigError(
            f"unknown top-level keys: {sorted(unknown_top)} (expected [gateway] and [sources.*])"
        )

    gateway = dict(data.get("gateway", {}))
    unknown_gw = set(gateway) - _GATEWAY_KEYS
    if unknown_gw:
        raise ConfigError(f"unknown [gateway] keys: {sorted(unknown_gw)}")

    sources = dict(BUILTIN_SOURCES)
    for source_id, overrides in dict(data.get("sources", {})).items():
        if source_id not in BUILTIN_SOURCES:
            raise ConfigError(f"unknown source {source_id!r}; known sources: {sorted(BUILTIN_SOURCES)}")
        sources[source_id] = _apply_overrides(BUILTIN_SOURCES[source_id], dict(overrides))

    return Settings(
        sources=sources,
        timeout_s=float(gateway.get("timeout_seconds", 45.0)),
        max_result_chars=int(gateway.get("max_result_chars", 12_000)),
        config_path=config_path,
    )


def _apply_overrides(spec: SourceSpec, overrides: dict[str, Any]) -> SourceSpec:
    where = f"[sources.{spec.id}]"
    unknown = set(overrides) - _SOURCE_KEYS
    for key in unknown:
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            raise ConfigError(
                f"{where}: {key!r} looks like a secret. Put tokens in environment variables and name them with token_env."
            )
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}; allowed: {sorted(_SOURCE_KEYS)}")

    changes: dict[str, Any] = {}
    if "enabled" in overrides:
        changes["enabled"] = bool(overrides["enabled"])
    if "transport" in overrides:
        if overrides["transport"] not in ("http", "stdio"):
            raise ConfigError(f"{where}: transport must be 'http' or 'stdio'")
        changes["transport"] = overrides["transport"]
    if "url" in overrides:
        changes["url"] = str(overrides["url"])
    if "headers" in overrides:
        headers = {str(k): str(v) for k, v in dict(overrides["headers"]).items()}
        secret = sorted(k for k in headers if k.lower() in _SECRET_HEADERS)
        if secret:
            raise ConfigError(f"{where}: header(s) {secret} carry credentials; use token_env instead")
        changes["headers"] = headers
    if "command" in overrides:
        changes["command"] = str(overrides["command"])
    if "args" in overrides:
        changes["args"] = tuple(str(a) for a in overrides["args"])
    if "env" in overrides:
        changes["env"] = {str(k): str(v) for k, v in dict(overrides["env"]).items()}
    if "token_env" in overrides:
        value = overrides["token_env"]
        changes["token_env"] = (str(value),) if isinstance(value, str) else tuple(str(v) for v in value)
    if "token_required" in overrides:
        changes["token_required"] = bool(overrides["token_required"])
    if "allowed_tools" in overrides:
        changes["allowed_tools"] = frozenset(str(t) for t in overrides["allowed_tools"])

    updated = spec.with_overrides(**changes)
    if updated.transport == "http" and not updated.url:
        raise ConfigError(f"{where}: transport 'http' needs url")
    if updated.transport == "stdio" and not updated.command:
        raise ConfigError(f"{where}: transport 'stdio' needs command")
    return updated
