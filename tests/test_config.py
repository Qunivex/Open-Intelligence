from __future__ import annotations

from pathlib import Path

import pytest

from open_intelligence.config import (
    ConfigError,
    load_settings,
    read_env_file,
    settings_from_mapping,
    token_environment,
)
from open_intelligence.sources import BUILTIN_SOURCES

EXAMPLE = Path(__file__).resolve().parent.parent / "open-intelligence.example.toml"


def test_defaults_without_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    settings = load_settings(environ={})
    assert set(settings.sources) == {"github", "huggingface", "kaggle"}
    assert settings.sources["github"].url == "https://api.githubcopilot.com/mcp/"
    assert settings.sources["github"].headers["X-MCP-Readonly"] == "true"
    assert settings.config_path is None


def test_example_file_loads() -> None:
    settings = load_settings(EXAMPLE, environ={})
    assert settings.sources["kaggle"].url == "https://www.kaggle.com/mcp"
    assert settings.sources["github"].token_required is True
    assert settings.timeout_s == 45


def test_switch_github_to_local_docker() -> None:
    settings = settings_from_mapping(
        {
            "sources": {
                "github": {
                    "transport": "stdio",
                    "command": "docker",
                    "args": ["run", "-i", "ghcr.io/github/github-mcp-server"],
                }
            }
        }
    )
    gh = settings.sources["github"]
    assert gh.transport == "stdio" and gh.command == "docker"
    assert gh.allowed_tools  # the allowlist is kept


@pytest.mark.parametrize(
    "overrides",
    [
        {"token": "ghp_x"},
        {"api_key": "x"},
        {"headers": {"Authorization": "Bearer x"}},
    ],
)
def test_secrets_are_refused_in_the_file(overrides: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        settings_from_mapping({"sources": {"github": overrides}})


def test_unknown_source_and_keys() -> None:
    with pytest.raises(ConfigError, match="unknown source"):
        settings_from_mapping({"sources": {"pypi": {}}})
    with pytest.raises(ConfigError, match="unknown keys"):
        settings_from_mapping({"sources": {"github": {"colour": "blue"}}})
    with pytest.raises(ConfigError, match="needs command"):
        settings_from_mapping({"sources": {"github": {"transport": "stdio"}}})


def test_ranking_defaults_and_overrides() -> None:
    defaults = settings_from_mapping({}).ranking
    assert defaults.weights == {"fit": 0.40, "quality": 0.20, "health": 0.15, "trust": 0.15, "adoption": 0.10}
    assert defaults.read_per_source == 8

    custom = settings_from_mapping(
        {
            "ranking": {
                "weights": {"fit": 3, "adoption": 0},
                "read_per_source": 4,
                "half_life_months": {"code": 12},
            }
        }
    ).ranking
    assert sum(custom.weights.values()) == pytest.approx(1.0)  # normalized
    assert custom.weights["adoption"] == 0
    assert custom.weights["fit"] > 0.8
    assert custom.read_per_source == 4
    assert custom.half_life_months["code"] == 12
    assert custom.half_life_months["dataset"] == 60


@pytest.mark.parametrize(
    "ranking",
    [
        {"colour": "blue"},
        {"weights": {"popularity": 1}},
        {"weights": {"fit": -1}},
        {"weights": {"fit": 0, "quality": 0, "health": 0, "trust": 0, "adoption": 0}},
        {"half_life_months": {"code": 0}},
    ],
)
def test_ranking_config_errors(ranking: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        settings_from_mapping({"ranking": ranking})


# --- tokens ---------------------------------------------------------------------------------------

FAKE_TOKEN = "github_pat_" + "x1Y2" * 12  # shaped like a token; not a real one


def test_a_token_pasted_where_a_variable_name_belongs_is_refused_without_echoing_it() -> None:
    for entries in (["GITHUB_PERSONAL_ACCESS_TOKEN", FAKE_TOKEN], FAKE_TOKEN, ["not a name"]):
        with pytest.raises(ConfigError) as error:
            settings_from_mapping({"sources": {"github": {"token_env": entries}}})
        assert "NAME of an environment variable" in str(error.value)
        assert FAKE_TOKEN not in str(error.value)
    renamed = settings_from_mapping({"sources": {"github": {"token_env": ["MY_GH_TOKEN"]}}})
    assert renamed.sources["github"].token_env == ("MY_GH_TOKEN",)


def test_env_file_lines(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        '# local secrets\n\nGITHUB_PERSONAL_ACCESS_TOKEN=abc123\nexport HF_TOKEN = "quoted value"\n'
        "KAGGLE_API_TOKEN='single'\nnot a pair\n1BAD=x\nEMPTY=\n",
        encoding="utf-8",
    )
    assert read_env_file(path) == {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "abc123",
        "HF_TOKEN": "quoted value",
        "KAGGLE_API_TOKEN": "single",
        "EMPTY": "",
    }
    assert read_env_file(tmp_path / "missing") == {}


def test_tokens_come_from_the_environment_first_then_from_a_dotenv_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "GITHUB_PERSONAL_ACCESS_TOKEN=from-file\nHF_TOKEN=from-file\nAWS_SECRET_ACCESS_KEY=unrelated\n",
        encoding="utf-8",
    )
    environment = token_environment(
        BUILTIN_SOURCES, base={"HF_TOKEN": "from-env", "PATH": "/bin"}, paths=[path]
    )
    assert environment["GITHUB_PERSONAL_ACCESS_TOKEN"] == "from-file"
    assert environment["HF_TOKEN"] == "from-env"  # what is already set wins
    assert "AWS_SECRET_ACCESS_KEY" not in environment  # only the tokens the sources name are read
    assert environment["PATH"] == "/bin"
    assert token_environment(BUILTIN_SOURCES, base={}, paths=[tmp_path / "missing"]) == {}
