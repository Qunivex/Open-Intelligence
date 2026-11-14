"""Calls the real servers. Deselected by default; run with: pytest -m live"""

from __future__ import annotations

import pytest

from open_intelligence.config import Settings
from open_intelligence.connector import Connector
from open_intelligence.sources import BUILTIN_SOURCES

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture
def live() -> Connector:
    return Connector(Settings(sources=dict(BUILTIN_SOURCES)))


async def test_huggingface_search(live: Connector) -> None:
    outcome = await live.call(
        "huggingface", "hub_repo_search", {"query": "sentiment", "repo_types": ["model"], "limit": 2}
    )
    assert outcome.ok, outcome.error


async def test_kaggle_dataset_search(live: Connector) -> None:
    outcome = await live.call(
        "kaggle", "search_datasets", {"request": {"search": "customer churn", "pageSize": 2}}
    )
    assert outcome.ok, outcome.error


async def test_github_reports_token_state(live: Connector) -> None:
    check = await live.check("github")
    assert check.connected is True or "token" in (check.error or "")
