"""User configuration: one TOML file, outside any project, that holds every API key.

Rules (from the design's "Source connections and credentials"):

* Only a person adds a key. No tool input or output carries a key, so an agent
  has nothing to leak or be tricked into revealing.
* Keys never live in the repo. Inline keys are refused when the config file
  sits inside a git work tree; ``{ env = ... }`` and ``{ keyring = ... }``
  references are offered for people who prefer not to store keys in a file.
* Secrets are wrapped in :class:`Secret`, whose ``repr``/``str`` are masked,
  and a logging filter scrubs any configured secret from log output.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP = "reuse-layer"
CONFIG_ENV = "REUSE_LAYER_CONFIG"
LICENSE_POLICIES = ("permissive", "weak_copyleft", "any_osi")
KNOWN_KEYS = ("github", "anthropic", "huggingface", "kaggle")


class ConfigError(Exception):
    """The config file is missing, malformed or unsafe."""


class Secret:
    """A credential. Never printed, never serialised, never sent to an agent."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('********')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))


# --------------------------------------------------------------------------- paths


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP


def default_config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    return Path(override).expanduser() if override else config_dir() / "config.toml"


def template_text() -> str:
    return resources.files("reuse_layer").joinpath("config.example.toml").read_text(encoding="utf-8")


def write_template(path: Path) -> Path:
    """Write the example config for a person to fill in. Never overwrites."""
    if path.exists():
        raise ConfigError(f"{path} already exists; edit it instead")
    if inside_git_repo(path):
        raise ConfigError(f"{path} is inside a git repository; keep the config outside your projects")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template_text(), encoding="utf-8")
    _restrict_permissions(path)
    return path


def _restrict_permissions(path: Path) -> None:
    if os.name == "posix":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600: owner only
    # On Windows, files under %APPDATA% inherit an owner-only ACL by default.


def inside_git_repo(path: Path) -> bool:
    """True when ``path`` is inside a git work tree (stops at the home directory)."""
    home = Path.home().resolve()
    p = path.expanduser().resolve().parent
    while True:
        if p == home or p.parent == p:
            return False
        if (p / ".git").exists():
            return True
        p = p.parent


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class GitHubSettings:
    api_url: str = "https://api.github.com"
    budget_ms: int = 2000
    deadline_ms: int = 9000
    max_candidates: int = 40
    deep_enrich: int = 8
    max_concurrency: int = 8


@dataclass(frozen=True)
class RankingSettings:
    relevance: float = 0.35
    trust: float = 0.25
    preference: float = 0.20
    verified: float = 0.10
    tier: float = 0.10
    explore: bool = True
    newer_than_days: int = 180
    build_yourself_below: float = 0.40


@dataclass(frozen=True)
class GateSettings:
    license_policy: str | tuple[str, ...] = "permissive"
    min_age_days: int = 30
    max_days_since_push: int = 730
    allow_forks: bool = False
    require_signed_releases: bool = False


@dataclass(frozen=True)
class RelevanceSettings:
    judge: str = "auto"
    model: str = "claude-haiku-5-5"
    timeout_s: float = 8.0


@dataclass(frozen=True)
class Settings:
    keys: dict[str, Secret] = field(default_factory=dict)
    github: GitHubSettings = field(default_factory=GitHubSettings)
    ranking: RankingSettings = field(default_factory=RankingSettings)
    gates: GateSettings = field(default_factory=GateSettings)
    relevance: RelevanceSettings = field(default_factory=RelevanceSettings)
    storage_path: Path | None = None
    retain_task_text: bool = False
    curated_owners: tuple[str, ...] = ()
    config_path: Path | None = None
    warnings: tuple[str, ...] = ()

    def secret(self, name: str) -> Secret | None:
        return self.keys.get(name)

    def db_path(self) -> Path:
        if self.storage_path is not None:
            return self.storage_path
        base = self.config_path.parent if self.config_path else config_dir()
        return base / "reuse-layer.sqlite3"

    @classmethod
    def load(cls, path: Path | None = None, *, required: bool = False) -> Settings:
        """Load the user's config. A missing file gives defaults (no keys) unless ``required``."""
        path = (path or default_config_path()).expanduser()
        if not path.exists():
            if required:
                raise ConfigError(f"no config file at {path}; create one with `reuse-layer config init`")
            return cls(config_path=path, warnings=(f"no config file at {path}; GitHub is not connected",))
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
        return cls.from_mapping(data, config_path=path)

    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, config_path: Path | None = None) -> Settings:
        warnings: list[str] = []
        keys = _load_keys(_table(data, "keys"), config_path, warnings)
        gh = _table(data, "github")
        rk = _table(data, "ranking")
        gt = _table(data, "gates")
        rl = _table(data, "relevance")
        st = _table(data, "storage")
        tiers = _table(data, "tiers")

        github = GitHubSettings(
            api_url=_https_url(_str(gh, "api_url", GitHubSettings.api_url)),
            budget_ms=_int(gh, "budget_ms", 2000, 200, 30_000),
            deadline_ms=_int(gh, "deadline_ms", 9000, 1000, 120_000),
            max_candidates=_int(gh, "max_candidates", 40, 5, 100),
            deep_enrich=_int(gh, "deep_enrich", 8, 3, 20),
            max_concurrency=_int(gh, "max_concurrency", 8, 1, 32),
        )
        ranking = RankingSettings(
            relevance=_float(rk, "relevance", 0.35),
            trust=_float(rk, "trust", 0.25),
            preference=_float(rk, "preference", 0.20),
            verified=_float(rk, "verified", 0.10),
            tier=_float(rk, "tier", 0.10),
            explore=_bool(rk, "explore", True),
            newer_than_days=_int(rk, "newer_than_days", 180, 0, 3650),
            build_yourself_below=_float(rk, "build_yourself_below", 0.40),
        )
        total = ranking.relevance + ranking.trust + ranking.preference + ranking.verified + ranking.tier
        if not 0.99 <= total <= 1.01:
            warnings.append(f"ranking weights sum to {total:.2f}, not 1.0; scores are normalised")

        policy_raw = gt.get("license_policy", "permissive")
        policy: str | tuple[str, ...]
        if isinstance(policy_raw, list):
            policy = tuple(str(x) for x in policy_raw if isinstance(x, str))
        elif isinstance(policy_raw, str) and policy_raw in LICENSE_POLICIES:
            policy = policy_raw
        else:
            raise ConfigError(f"gates.license_policy must be one of {LICENSE_POLICIES} or a list of SPDX ids")
        gates = GateSettings(
            license_policy=policy,
            min_age_days=_int(gt, "min_age_days", 30, 0, 3650),
            max_days_since_push=_int(gt, "max_days_since_push", 730, 30, 7300),
            allow_forks=_bool(gt, "allow_forks", False),
            require_signed_releases=_bool(gt, "require_signed_releases", False),
        )
        judge = _str(rl, "judge", "auto")
        if judge not in ("auto", "lexical", "llm"):
            raise ConfigError("relevance.judge must be auto, lexical or llm")
        relevance = RelevanceSettings(
            judge=judge,
            model=_str(rl, "model", "claude-haiku-5-5"),
            timeout_s=float(_int(rl, "timeout_s", 8, 1, 60)),
        )
        storage = _str(st, "path", "")
        owners = tiers.get("curated_owners", [])
        return cls(
            keys=keys,
            github=github,
            ranking=ranking,
            gates=gates,
            relevance=relevance,
            storage_path=Path(storage).expanduser() if storage else None,
            retain_task_text=_bool(st, "retain_task_text", False),
            curated_owners=tuple(str(o).lower() for o in owners if isinstance(o, str)),
            config_path=config_path,
            warnings=tuple(warnings),
        )


# --------------------------------------------------------------------------- keys


def _load_keys(table: dict[str, Any], path: Path | None, warnings: list[str]) -> dict[str, Secret]:
    keys: dict[str, Secret] = {}
    in_repo = path is not None and inside_git_repo(path)
    for name, raw in table.items():
        if name not in KNOWN_KEYS:
            warnings.append(f"keys.{name} is not used by this version")
        value: str | None = None
        if isinstance(raw, str):
            if not raw.strip():
                continue
            if in_repo:
                raise ConfigError(
                    f"{path} is inside a git repository and holds an inline key ({name}). Move the config "
                    "outside your projects (see `reuse-layer config path`) or use { env = ... } / { keyring = ... }."
                )
            value = raw.strip()
            _warn_if_readable(path, warnings)
        elif isinstance(raw, dict) and "env" in raw:
            value = os.environ.get(str(raw["env"]), "").strip() or None
            if value is None:
                warnings.append(f"keys.{name}: environment variable {raw['env']} is not set")
        elif isinstance(raw, dict) and "keyring" in raw:
            value = _from_keyring(str(raw["keyring"]), name, warnings)
        else:
            raise ConfigError(f'keys.{name} must be a string, {{ env = "VAR" }} or {{ keyring = "service" }}')
        if value:
            if any(c.isspace() for c in value) or len(value) > 512:
                raise ConfigError(f"keys.{name} does not look like an API key")
            keys[name] = Secret(value)
    if keys:
        install_redaction(keys.values())
    return keys


def _from_keyring(service: str, name: str, warnings: list[str]) -> str | None:
    try:
        import keyring
    except ImportError:
        warnings.append(f"keys.{name} uses the keychain but `keyring` is not installed (pip install keyring)")
        return None
    try:
        value = keyring.get_password(service, name)
    except Exception:  # keychain backends raise many different errors
        warnings.append(f"keys.{name}: could not read the OS keychain")
        return None
    if not value:
        warnings.append(f"keys.{name}: nothing stored in the keychain (run `reuse-layer keys set {name}`)")
    return str(value) if value else None


def store_in_keyring(service: str, name: str, value: str) -> None:
    try:
        import keyring
    except ImportError as exc:
        raise ConfigError("`keyring` is not installed; run: pip install keyring") from exc
    keyring.set_password(service, name, value)


def _warn_if_readable(path: Path | None, warnings: list[str]) -> None:
    if path is None or os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        warnings.append(f"{path} holds keys and is readable by other users; run: chmod 600 {path}")


class _RedactSecrets(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.values: set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.values:
            return True
        msg = record.getMessage()
        if any(v in msg for v in self.values):
            for v in self.values:
                msg = msg.replace(v, "********")
            record.msg, record.args = msg, None
        return True


_REDACTOR = _RedactSecrets()


def install_redaction(secrets: Any) -> None:
    for s in secrets:
        if isinstance(s, Secret) and len(s.reveal()) >= 8:
            _REDACTOR.values.add(s.reveal())
    root = logging.getLogger()
    for h in root.handlers:
        if _REDACTOR not in h.filters:
            h.addFilter(_REDACTOR)
    if _REDACTOR not in root.filters:
        root.addFilter(_REDACTOR)


def redaction_filter() -> logging.Filter:
    return _REDACTOR


# --------------------------------------------------------------------------- helpers


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _str(t: dict[str, Any], k: str, default: str) -> str:
    v = t.get(k, default)
    if not isinstance(v, str):
        raise ConfigError(f"{k} must be a string")
    return v


def _int(t: dict[str, Any], k: str, default: int, lo: int, hi: int) -> int:
    v = t.get(k, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ConfigError(f"{k} must be an integer between {lo} and {hi}")
    return v


def _float(t: dict[str, Any], k: str, default: float) -> float:
    v = t.get(k, default)
    if isinstance(v, bool) or not isinstance(v, int | float) or not 0 <= float(v) <= 1:
        raise ConfigError(f"{k} must be a number between 0 and 1")
    return float(v)


def _bool(t: dict[str, Any], k: str, default: bool) -> bool:
    v = t.get(k, default)
    if not isinstance(v, bool):
        raise ConfigError(f"{k} must be true or false")
    return v


def _https_url(url: str) -> str:
    url = url.rstrip("/")
    if not url.startswith("https://"):
        raise ConfigError("github.api_url must be an https:// URL")
    return url
