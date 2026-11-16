from __future__ import annotations

import json

import pytest

from open_intelligence.connector import Connector, SourceError
from tests.conftest import SECRET, fake_settings

pytestmark = pytest.mark.anyio


async def test_lists_only_allowed_tools(connector: Connector) -> None:
    names = {t.name for t in await connector.list_tools("github")}
    assert names == {"search_repositories", "get_file_contents"}  # create_issue is offered but not allowed
    kaggle = {t.name for t in await connector.list_tools("kaggle")}
    assert "download_dataset" not in kaggle
    assert {"search_datasets", "list_models", "search_notebooks"} <= kaggle


async def test_call_passes_arguments_and_token_without_leaking_it(connector: Connector) -> None:
    outcome = await connector.call("github", "search_repositories", {"query": "reverse etl", "perPage": 3})
    assert outcome.ok, outcome.error
    echoed = json.loads(outcome.text)
    assert echoed["args"] == {"query": "reverse etl", "perPage": 3}
    assert echoed["token_present"] is True
    assert SECRET not in json.dumps(outcome.as_dict())


async def test_write_tools_are_refused_before_any_connection(connector: Connector) -> None:
    outcome = await connector.call("github", "create_issue", {"owner": "a", "repo": "b", "title": "x"})
    assert not outcome.ok
    assert "not allowed" in (outcome.error or "")
    outcome = await connector.call("kaggle", "download_dataset", {"request": {}})
    assert not outcome.ok and "read-only" in (outcome.error or "")


async def test_missing_required_token_is_reported_without_connecting() -> None:
    connector = Connector(fake_settings(), environ={})
    check = await connector.check("github")
    assert check.connected is False
    assert "FAKE_GH_TOKEN" in (check.error or "")
    outcome = await connector.call("github", "search_repositories", {"query": "x"})
    assert not outcome.ok and "needs a token" in (outcome.error or "")
    # Sources with optional tokens still work anonymously.
    assert (await connector.call("huggingface", "hub_repo_search", {"query": "x"})).ok


async def test_upstream_tool_errors_are_returned_not_raised(connector: Connector) -> None:
    outcomes = await connector.call_many(
        "huggingface",
        [
            ("hub_repo_details", {"repo_ids": ["explode/please"]}),
            ("hub_repo_details", {"repo_ids": ["ok/repo"]}),
        ],
    )
    assert not outcomes[0].ok and "error" in (outcomes[0].error or "")
    assert outcomes[1].ok  # one failing call does not sink the others on the same connection


async def test_output_is_capped() -> None:
    connector = Connector(fake_settings(max_result_chars=1000), environ={})
    outcome = await connector.call("kaggle", "big_output", {"request": {}})
    assert outcome.ok and outcome.truncated
    assert len(outcome.text) < 1100


async def test_unreachable_source_becomes_a_clean_error() -> None:
    settings = fake_settings()
    settings.sources["huggingface"] = settings.sources["huggingface"].with_overrides(
        command="definitely-not-a-real-command-xyz", args=()
    )
    connector = Connector(settings, environ={})
    check = await connector.check("huggingface")
    assert check.connected is False and "could not reach huggingface" in (check.error or "")
    with pytest.raises(SourceError):
        await connector.list_tools("huggingface")


async def test_check_reports_auth_and_tools_but_never_the_token(connector: Connector) -> None:
    check = (await connector.check("github")).as_dict()
    assert check["connected"] is True
    assert check["auth"] == "token from $FAKE_GH_TOKEN"
    assert "search_repositories" in check["tools"]
    assert "list_releases" in check["allowed_but_not_offered"]
    assert SECRET not in json.dumps(check)


async def test_disabled_source() -> None:
    settings = fake_settings()
    settings.sources["kaggle"] = settings.sources["kaggle"].with_overrides(enabled=False)
    check = await Connector(settings, environ={}).check("kaggle")
    assert check.enabled is False and "disabled" in (check.error or "")


async def test_unknown_source() -> None:
    with pytest.raises(SourceError, match="unknown source"):
        Connector(fake_settings(), environ={}).spec("pypi")
