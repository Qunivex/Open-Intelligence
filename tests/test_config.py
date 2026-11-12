"""The user config file: keys, references, safety refusals and redaction."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import pytest

from reuse_layer.config import (
    ConfigError,
    Secret,
    Settings,
    install_redaction,
    redaction_filter,
    template_text,
    write_template,
)

KEY = "github_pat_example_0123456789abcdef"


def _write(tmp: Path, body: str) -> Path:
    p = tmp / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_template_is_valid_and_has_no_keys() -> None:
    s = Settings.from_mapping(tomllib.loads(template_text()))
    assert s.keys == {}
    assert s.ranking.relevance == 0.35 and s.ranking.trust == 0.25 and s.ranking.preference == 0.20
    assert s.ranking.verified == 0.10 and s.ranking.tier == 0.10
    assert s.gates.license_policy == "permissive" and s.gates.min_age_days == 30
    assert s.github.budget_ms == 5000 and s.github.deadline_ms == 30000
    assert s.relevance.model == "claude-haiku-5-5"


def test_inline_key_is_wrapped_and_masked(tmp_path: Path) -> None:
    s = Settings.load(_write(tmp_path, f'[keys]\ngithub = "{KEY}"\n'))
    secret = s.secret("github")
    assert isinstance(secret, Secret) and secret.reveal() == KEY
    assert KEY not in repr(secret) and KEY not in str(secret) and KEY not in repr(s)


def test_env_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_GH", KEY)
    s = Settings.load(_write(tmp_path, '[keys]\ngithub = { env = "MY_GH" }\n'))
    assert s.secret("github") == Secret(KEY)


def test_missing_env_reference_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE_GH", raising=False)
    s = Settings.load(_write(tmp_path, '[keys]\ngithub = { env = "NOPE_GH" }\n'))
    assert s.secret("github") is None
    assert any("NOPE_GH" in w for w in s.warnings)


def test_inline_key_refused_inside_a_git_repo(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ConfigError, match="inside a git repository"):
        Settings.load(_write(repo, f'[keys]\ngithub = "{KEY}"\n'))


def test_write_template_never_overwrites_or_writes_into_a_repo(tmp_path: Path) -> None:
    p = write_template(tmp_path / "cfg" / "config.toml")
    assert p.read_text(encoding="utf-8") == template_text()
    with pytest.raises(ConfigError, match="already exists"):
        write_template(p)
    (tmp_path / "proj" / ".git").mkdir(parents=True)
    with pytest.raises(ConfigError, match="git repository"):
        write_template(tmp_path / "proj" / "config.toml")


def test_missing_file_gives_defaults_unless_required(tmp_path: Path) -> None:
    s = Settings.load(tmp_path / "absent.toml")
    assert s.keys == {} and s.warnings
    with pytest.raises(ConfigError):
        Settings.load(tmp_path / "absent.toml", required=True)


@pytest.mark.parametrize(
    "body",
    [
        '[gates]\nlicense_policy = "anything"\n',
        '[github]\napi_url = "http://api.github.com"\n',
        "[ranking]\nrelevance = 1.5\n",
        '[keys]\ngithub = "has space inside"\n',
        "[keys]\ngithub = 42\n",
    ],
)
def test_bad_values_are_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        Settings.load(_write(tmp_path, body))


def test_license_policy_list(tmp_path: Path) -> None:
    s = Settings.load(_write(tmp_path, '[gates]\nlicense_policy = ["MIT", "Apache-2.0"]\n'))
    assert s.gates.license_policy == ("MIT", "Apache-2.0")


def test_redaction_scrubs_secrets_from_logs(caplog: pytest.LogCaptureFixture) -> None:
    install_redaction([Secret(KEY)])
    logger = logging.getLogger("reuse_layer.test")
    caplog.handler.addFilter(redaction_filter())
    with caplog.at_level(logging.WARNING):
        logger.warning("token is %s", KEY)
    assert KEY not in caplog.text and "********" in caplog.text
