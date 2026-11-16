"""Calls the real servers. Deselected by default; run with: pytest -m live"""

from __future__ import annotations

import pytest

from open_intelligence.config import Settings
from open_intelligence.connector import Connector
from open_intelligence.ranking import Constraints, Ranker, RankRequest
from open_intelligence.ranking.adapters import ADAPTERS
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


async def test_parsers_still_read_the_live_formats(live: Connector) -> None:
    """Hugging Face answers in Markdown written for a model to read. If that format drifts, the
    parsers return less: this is the alarm."""
    adapter = ADAPTERS[("huggingface", "model")]
    call = adapter.search_calls(["sentiment"], 5)[0]
    outcome = (await live.call_many("huggingface", [(call.tool, call.arguments)], full_text=True))[0]
    assert outcome.ok, outcome.error
    found = adapter.parse_search(outcome, 5)
    assert len(found) == 5
    assert all("downloads" in c.metrics and c.tags and c.task for c in found)

    reads = adapter.enrich_calls(found[:2])
    outcomes = await live.call_many("huggingface", [(c.tool, c.arguments) for c in reads], full_text=True)
    by_id = {c.id: c for c in found}
    for read, result in zip(reads, outcomes, strict=True):
        adapter.apply(by_id, read, result)
    for c in found[:2]:
        assert c.enriched, c.read_notes
        assert c.updated_at is not None
        assert c.weight_formats is not None

    datasets = ADAPTERS[("kaggle", "dataset")]
    call = datasets.search_calls(["customer churn"], 5)[0]
    outcome = (await live.call_many("kaggle", [(call.tool, call.arguments)], full_text=True))[0]
    found = datasets.parse_search(outcome, 5)
    assert len(found) == 5
    assert all({"downloads", "notebooks", "usability"} <= set(c.metrics) for c in found)


async def test_ranking_end_to_end(live: Connector) -> None:
    request = RankRequest(
        task="Find a German NER dataset to fine-tune a token classifier",
        query="german ner",
        needs=["dataset"],
        queries={"dataset": ["german ner", "germeval"]},
        must_have={"dataset": ["named entity recognition", "german"]},
        constraints=Constraints(languages=["de"]),
    )
    result = await Ranker(live).find(request)
    (datasets,) = result["results"]
    assert datasets["verdict"] == "picks", datasets
    assert all(s["ok"] for s in datasets["sources"]), datasets["sources"]
    assert datasets["read_in_depth"] > 0
    for pick in datasets["picks"]:
        assert pick["why"]
        assert pick["covers"]


async def test_security_evidence_for_a_well_known_repository() -> None:
    from open_intelligence.ranking.evidence import SecurityLookup
    from open_intelligence.ranking.models import Candidate

    flask = Candidate(
        source="github", need="code", id="pallets/flask", url="u", programming_language="Python"
    )
    other = Candidate(
        source="github", need="code", id="pytorch/pytorch", url="u", programming_language="Python"
    )
    await SecurityLookup().lookup_many([flask, other])
    assert flask.security is not None
    assert flask.security.scorecard is not None and 0 <= flask.security.scorecard <= 10
    assert flask.security.package == "pypi:flask"
    assert flask.security.advisories is not None  # checked, whatever the answer
    # pytorch/pytorch publishes "torch": no same-named package points back, so nothing is claimed.
    assert other.security is not None
    assert other.security.package is None
    assert other.security.advisories is None
