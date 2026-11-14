"""The whole pipeline, offline: fake upstream servers answer with the recorded fixtures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from open_intelligence.connector import Connector
from open_intelligence.ranking import Constraints, Ranker, RankRequest
from open_intelligence.ranking.engine import _questions
from open_intelligence.ranking.models import Candidate
from open_intelligence.ranking.scoring import PARTS, Scored
from tests.conftest import SECRET, ranked

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 13, tzinfo=UTC)
ACME = "example-acme/census-snowflake-sync"


async def find(connector: Connector, **request: Any) -> dict[str, Any]:
    request.setdefault("constraints", Constraints())
    return await Ranker(connector, now=lambda: NOW).find(RankRequest(**request))


def need(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(r for r in result["results"] if r["need"] == name)


async def test_code_request_gates_scores_and_explains(ranked_connector: Connector) -> None:
    result = await find(
        ranked_connector,
        task="Python project to automate a Snowflake pipeline that sends data from Census to Snowflake",
        query="census snowflake",
        must_have={"code": ["census api", "snowflake load", "scheduling"]},
        constraints=Constraints(programming_language="python"),
    )
    assert [r["need"] for r in result["results"]] == ["code"]  # routed to GitHub only
    code = need(result, "code")
    assert code["verdict"] == "picks"
    (pick,) = code["picks"]
    assert pick["id"] == ACME
    assert pick["rank"] == 1
    assert pick["covers"] == ["census api", "snowflake load", "scheduling"]
    assert pick["missing"] == []
    assert set(pick["scores"]) == set(PARTS)
    assert pick["facts"]["license"] == "MIT"
    assert pick["facts"]["latest_release"] == "2026-10-18"
    assert pick["platform_position"] == 1
    # The fork with the same name is not a second option.
    assert pick["also_available_as"] == ["example-forky/census-snowflake-sync"]
    assert "written in Python" in pick["why"]
    assert any("vulnerabilities were not checked" in note for note in pick["unresolved_checks"])

    rejected = {r["id"]: r["gate"] for r in code["rejected"]}
    assert rejected == {
        "example-oldco/snowflake-census-loader": "archived",
        "example-dataflow/reverse-etl": "fit",
        "example-hype/snowflake-pipeline-pro": "fit",
    }
    assert code["platform_top"]["github"][0] == ACME
    assert code["considered"] == 5
    assert result["constraints"]["given"] == {"programming_language": "python"}


async def test_popular_but_hollow_repository_ranks_below_the_documented_one(
    ranked_connector: Connector,
) -> None:
    result = await find(
        ranked_connector, task="Build a Snowflake Census pipeline", query="snowflake census pipeline"
    )
    picks = {p["id"]: p for p in need(result, "code")["picks"]}
    hype = picks["example-hype/snowflake-pipeline-pro"]  # 5,000 stars, 12 forks, no license, empty README
    assert picks[ACME]["rank"] == 1
    assert hype["rank"] > 1
    assert any("stars may be inflated" in w for w in hype["warnings"])
    assert any("license is unclear" in note for note in hype["unresolved_checks"])
    assert hype["scores"]["quality"] < picks[ACME]["scores"]["quality"]


async def test_missing_github_token_is_inconclusive_not_empty() -> None:
    connector = ranked(environ={})
    result = await find(connector, task="Build a Snowflake Census pipeline", query="census snowflake")
    code = need(result, "code")
    assert code["verdict"] == "inconclusive"
    assert code["picks"] == []
    assert "needs a token" in code["sources"][0]["errors"][0]


async def test_models_from_two_sources_share_one_ranking(ranked_connector: Connector) -> None:
    result = await find(
        ranked_connector,
        task="Classify the sentiment of English tweets with a pretrained model",
        query="sentiment",
        needs=["model"],
        constraints=Constraints(languages=["en"]),
    )
    models = need(result, "model")
    assert models["verdict"] == "picks"
    assert {s["source"]: s["ok"] for s in models["sources"]} == {"huggingface": True, "kaggle": True}
    picks = models["picks"]
    assert [p["rank"] for p in picks] == [1, 2, 3]
    assert len({p["id"] for p in picks}) == 3
    assert [p["score"] for p in picks] == sorted((p["score"] for p in picks), reverse=True)
    for pick in picks:
        assert pick["why"]
        assert 0 <= pick["score"] <= 100

    # Pickle-only models are excluded once the file list confirms it, and the result says so.
    pickle_only = "cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual"
    assert pickle_only not in {p["id"] for p in picks}
    assert {"id": pickle_only, "gate": "weights"}.items() <= next(
        r for r in models["rejected"] if r["id"] == pickle_only
    ).items()
    assert any("pickle" in note for note in result["constraints"]["assumed"])
    # What the platform alone would have put first is shown next to the picks.
    assert models["platform_top"]["huggingface"][0].startswith("Xenova/")
    assert picks[0]["id"] != models["platform_top"]["huggingface"][0]


async def test_allowing_pickle_weights_removes_that_gate(ranked_connector: Connector) -> None:
    result = await find(
        ranked_connector,
        task="Classify sentiment with a pretrained model",
        query="sentiment",
        needs=["model"],
        constraints=Constraints(allow_pickle_weights=True),
    )
    assert all(r["gate"] != "weights" for r in need(result, "model")["rejected"])


async def test_age_limit_is_a_gate(ranked_connector: Connector) -> None:
    result = await find(
        ranked_connector,
        task="Classify sentiment with a pretrained model",
        query="sentiment",
        needs=["model"],
        constraints=Constraints(max_age_months=12),
    )
    models = need(result, "model")
    assert any(r["gate"] == "age" for r in models["rejected"])
    for pick in models["picks"]:
        assert pick["facts"]["updated"] >= "2025-11-13"


async def test_datasets_ranked_and_notebooks_inconclusive_without_a_kaggle_token(
    ranked_connector: Connector,
) -> None:
    result = await find(
        ranked_connector,
        task="Find customer churn data and worked notebooks",
        query="customer churn",
        needs=["dataset", "notebook"],
    )
    datasets = need(result, "dataset")
    assert datasets["verdict"] == "picks"
    assert {p["source"] for p in datasets["picks"]} <= {"huggingface", "kaggle"}
    kaggle_pick = next((p for p in datasets["picks"] if p["source"] == "kaggle"), None)
    if kaggle_pick is not None:
        assert any("usability" in reason for reason in kaggle_pick["why"])
    # Telco Customer Churn is Kaggle's first result; its license is not a grant, and the pick says so.
    everything = datasets["picks"] + datasets["runners_up"]
    assert "blastchar/telco-customer-churn" in {e["id"] for e in everything}

    notebooks = need(result, "notebook")
    assert notebooks["verdict"] == "inconclusive"
    assert "token" in notebooks["sources"][0]["errors"][0]


async def test_top_limits_the_picks_and_no_token_leaks(ranked_connector: Connector) -> None:
    result = await find(
        ranked_connector, task="Find customer churn data", query="customer churn", needs=["dataset"], top=1
    )
    assert len(need(result, "dataset")["picks"]) == 1
    assert SECRET not in json.dumps(result)


async def test_no_query_for_a_need_means_it_is_not_searched(ranked_connector: Connector) -> None:
    result = await find(ranked_connector, task="Find customer churn data", query="", needs=["dataset"])
    assert need(result, "dataset")["verdict"] == "not_searched"


def test_questions_are_asked_only_when_the_answer_would_change_the_result() -> None:
    def scored(**fields: Any) -> Scored:
        base = {"source": "huggingface", "need": "model", "id": "a/b", "url": "u"}
        return Scored(candidate=Candidate(**{**base, **fields}))

    plain = scored(license="apache-2.0", license_reported=True, size_bytes=int(5e8))
    assert _questions(Constraints(), [plain], [plain]) == []

    restricted = scored(id="lab/nc-model", license="cc-by-nc-4.0", license_reported=True)
    (question,) = _questions(Constraints(), [plain], [plain, restricted])
    assert question.startswith("Will this be used commercially?")
    assert "lab/nc-model" in question
    assert _questions(Constraints(commercial_use=False), [plain], [plain, restricted]) == []

    large = scored(id="lab/big", license="mit", license_reported=True, size_bytes=int(9e9))
    (question,) = _questions(Constraints(), [large], [large])
    assert question.startswith("Is there a hardware limit?")
    assert _questions(Constraints(max_model_size_gb=24), [large], [large]) == []

    python = scored(source="github", need="code", id="a/py", license="mit", programming_language="Python")
    go = scored(source="github", need="code", id="a/go", license="mit", programming_language="Go")
    (question,) = _questions(Constraints(), [python, go], [python, go])
    assert question.startswith("Which programming language")
