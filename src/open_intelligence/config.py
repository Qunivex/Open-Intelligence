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
from dataclasses import dataclass, field
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
_RANKING_KEYS = {"weights", "per_query", "max_queries", "read_per_source", "min_fit", "half_life_months"}
_WEIGHT_KEYS = ("fit", "quality", "health", "trust", "adoption")
_NEED_KEYS = ("code", "model", "dataset", "notebook")
_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey")
_SECRET_HEADERS = {"authorization", "cookie", "x-api-key"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RankingSettings:
    """Knobs of the ranking pipeline. The defaults are a first hypothesis, not tuned values."""

    # How much each part of the score counts. Normalized to sum to 1.
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "fit": 0.40,
            "quality": 0.20,
            "health": 0.15,
            "trust": 0.15,
            "adoption": 0.10,
        }
    )
    per_query: int = 10  # results asked of a source per query
    max_queries: int = 3  # phrasings used per need
    read_per_source: int = 8  # candidates per need and source whose README / card is read
    min_fit: float = 0.3  # below this fit a candidate is not offered at all
    # Months after which freshness halves: code goes stale faster than data.
    half_life_months: dict[str, float] = field(
        default_factory=lambda: {"code": 18.0, "model": 30.0, "dataset": 60.0, "notebook": 30.0}
    )


@dataclass(frozen=True)
class Settings:
    sources: dict[str, SourceSpec]
    timeout_s: float = 45.0
    max_result_chars: int = 12_000
    config_path: Path | None = None
    ranking: RankingSettings = field(default_factory=RankingSettings)


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
    unknown_top = set(data) - {"gateway", "sources", "ranking"}
    if unknown_top:
        raise ConfigError(
            f"unknown top-level keys: {sorted(unknown_top)} (expected [gateway], [ranking] and [sources.*])"
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
        ranking=_ranking_settings(dict(data.get("ranking", {}))),
    )


def _ranking_settings(table: dict[str, Any]) -> RankingSettings:
    unknown = set(table) - _RANKING_KEYS
    if unknown:
        raise ConfigError(f"unknown [ranking] keys: {sorted(unknown)}; allowed: {sorted(_RANKING_KEYS)}")
    defaults = RankingSettings()

    def numbers(key: str, allowed: tuple[str, ...], base: dict[str, float]) -> dict[str, float]:
        given = dict(table.get(key, {}))
        bad = set(given) - set(allowed)
        if bad:
            raise ConfigError(f"[ranking.{key}]: unknown keys {sorted(bad)}; allowed: {list(allowed)}")
        try:
            merged = {**base, **{k: float(v) for k, v in given.items()}}
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"[ranking.{key}]: values must be numbers") from exc
        if any(v < 0 for v in merged.values()):
            raise ConfigError(f"[ranking.{key}]: values must not be negative")
        return merged

    weights = numbers("weights", _WEIGHT_KEYS, defaults.weights)
    total = sum(weights.values())
    if total <= 0:
        raise ConfigError("[ranking.weights]: at least one weight must be above zero")
    half_life = numbers("half_life_months", _NEED_KEYS, defaults.half_life_months)
    if any(v <= 0 for v in half_life.values()):
        raise ConfigError("[ranking.half_life_months]: values must be above zero")
    return RankingSettings(
        weights={k: v / total for k, v in weights.items()},
        per_query=max(1, min(int(table.get("per_query", defaults.per_query)), 25)),
        max_queries=max(1, min(int(table.get("max_queries", defaults.max_queries)), 5)),
        read_per_source=max(1, min(int(table.get("read_per_source", defaults.read_per_source)), 15)),
        min_fit=max(0.0, min(float(table.get("min_fit", defaults.min_fit)), 1.0)),
        half_life_months=half_life,
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
