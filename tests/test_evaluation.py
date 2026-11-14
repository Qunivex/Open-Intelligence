from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from open_intelligence.connector import CallOutcome, Connector
from open_intelligence.evaluation import Recorder, evaluate, load_golden, render_markdown
from open_intelligence.evaluation.golden import GoldenError, golden_from_mapping
from open_intelligence.evaluation.metrics import judged_at_k, ndcg_at_k, precision_at_k
from open_intelligence.ranking.scoring import PARTS

ACME = "github:example-acme/census-snowflake-sync"
GOLDEN = {
    "meta": {"name": "test", "judged_by": "the test author"},
    "tasks": [
        {
            "id": "census-to-snowflake",
            "need": "code",
            "task": "Python project to automate a Snowflake pipeline that sends data from Census to Snowflake",
            "queries": ["census snowflake"],
            "must_have": ["census api", "snowflake load"],
            "constraints": {"programming_language": "python"},
            "judgments": {
                ACME: 3,
                "github:example-dataflow/reverse-etl": 1,
                "github:example-oldco/snowflake-census-loader": 0,
                "github:example-hype/snowflake-pipeline-pro": 0,
            },
        },
        {
            "id": "customer-churn-data",
            "need": "dataset",
            "task": "Find customer churn data to train a classifier",
            "queries": ["customer churn"],
            "judgments": {
                "kaggle:blastchar/telco-customer-churn": 3,
                "kaggle:muhammadshahidazeem/customer-churn-dataset": 2,
                "huggingface:aai510-group1/telco-customer-churn": 2,
                "huggingface:d0r1h/customer_churn": 1,
                "huggingface:kalhosni/CustomerChurnTelecom": 0,
                "huggingface:krisnadwipaj/customer-churn": 0,
            },
        },
        {
            "id": "churn-notebooks",
            "need": "notebook",
            "task": "Find worked notebooks on customer churn",
            "queries": ["customer churn"],
            "judgments": {"kaggle:someone/churn-eda": 2},
        },
    ],
}


# --- metrics --------------------------------------------------------------------------------------


def test_ndcg() -> None:
    judgments = {"a": 3, "b": 2, "c": 1, "d": 0}
    assert ndcg_at_k(["a", "b", "c"], judgments) == pytest.approx(1.0)
    assert ndcg_at_k(["c", "b", "a"], judgments) < ndcg_at_k(["a", "c", "b"], judgments) < 1.0
    assert ndcg_at_k(["d", "x", "y"], judgments) == 0.0  # an unjudged candidate counts as 0
    assert ndcg_at_k([], judgments) == 0.0
    # One perfect pick out of three slots: good, but not as good as filling the slots.
    assert ndcg_at_k(["a"], judgments) == pytest.approx(7 / (7 + 3 / 1.584962500721156 + 1 / 2))
    assert ndcg_at_k(["a"], {"a": 0}) is None  # nothing worth finding: the task cannot be scored


def test_precision_and_judged_share() -> None:
    judgments = {"a": 3, "b": 2, "c": 1}
    assert precision_at_k(["a", "b", "c"], judgments) == pytest.approx(2 / 3)
    assert precision_at_k(["a"], judgments) == pytest.approx(1 / 3)  # an empty slot is a miss
    assert judged_at_k(["a", "x", "y"], judgments) == pytest.approx(1 / 3)
    assert judged_at_k([], judgments) == 0.0


# --- golden set -----------------------------------------------------------------------------------


def test_golden_set_is_validated() -> None:
    golden = golden_from_mapping(GOLDEN)
    assert [t.id for t in golden.tasks] == ["census-to-snowflake", "customer-churn-data", "churn-notebooks"]
    request = golden.tasks[0].request()
    assert request.needs == ["code"]
    assert request.queries == {"code": ["census snowflake"]}
    assert request.must_have == {"code": ["census api", "snowflake load"]}
    assert request.constraints.programming_language == "python"

    def broken(**changes: Any) -> dict[str, Any]:
        return {"tasks": [{**GOLDEN["tasks"][0], **changes}]}  # type: ignore[dict-item]

    for bad in (
        broken(need="weights"),
        broken(queries=[]),
        broken(judgments={}),
        broken(judgments={"example-acme/census-snowflake-sync": 3}),  # no source prefix
        broken(judgments={ACME: 5}),
        broken(judgments={ACME: True}),
        broken(constraints={"budget": "low"}),
        {"tasks": [GOLDEN["tasks"][0], GOLDEN["tasks"][0]]},  # duplicate id
        {"tasks": []},
    ):
        with pytest.raises(GoldenError):
            golden_from_mapping(bad)


def test_the_seed_set_shipped_with_the_package_loads() -> None:
    golden = load_golden()
    assert golden.name == "seed"
    assert len(golden.tasks) >= 8
    assert "practitioner" in golden.judged_by  # it says who judged it, and that it needs review
    assert {t.need for t in golden.tasks} >= {"model", "dataset", "code"}
    for task in golden.tasks:
        assert max(task.judgments.values()) >= 2, f"{task.id} has no good candidate to find"


def test_missing_golden_file() -> None:
    with pytest.raises(GoldenError, match="cannot read"):
        load_golden("does/not/exist.toml")


# --- runner ---------------------------------------------------------------------------------------

pytestmark_async = pytest.mark.anyio


@pytestmark_async
async def test_report_scores_the_ranking_and_each_platforms_own_order(ranked_connector: Connector) -> None:
    recorder = Recorder(ranked_connector)
    report = await evaluate(golden_from_mapping(GOLDEN), recorder, now=recorder.now, ablations=True)
    by_id = {t.id: t for t in report.tasks}

    code = by_id["census-to-snowflake"]
    assert code.ranked == [ACME]
    assert code.scores is not None
    # One pick, and it is the best one: the two empty slots cost a little against an ideal top 3.
    assert code.scores.ndcg == pytest.approx(7 / (7 + 1 / 1.584962500721156))
    assert code.scores.precision == pytest.approx(1 / 3)
    assert code.scores.judged == 1.0
    github_top, github_scores = code.platforms["github"]
    assert github_top[0] == ACME
    assert github_scores.ndcg == pytest.approx(1.0)  # GitHub's own order happens to be ideal here

    data = by_id["customer-churn-data"]
    assert data.scores is not None and data.scores.ndcg is not None
    assert set(data.platforms) == {"huggingface", "kaggle"}
    assert data.best_platform == max(s.ndcg for _, s in data.platforms.values() if s.ndcg is not None)

    skipped = by_id["churn-notebooks"]
    assert skipped.scores is None
    assert skipped.verdict == "inconclusive"
    assert "token" in skipped.note

    assert [t.id for t in report.scored] == ["census-to-snowflake", "customer-churn-data"]
    assert report.ranking_ndcg == pytest.approx((code.scores.ndcg + data.scores.ndcg) / 2)
    assert set(report.ablations) == set(PARTS)
    assert all(value is not None for value in report.ablations.values())

    summary = report.as_dict()
    json.dumps(summary)  # serializable
    assert summary["tasks_scored"] == 2
    assert summary["tasks_skipped"] == 1
    assert summary["mean_ndcg_at_3"]["best_platform_order"] is not None

    table = render_markdown(report)
    assert "| census-to-snowflake | code |" in table
    assert "| churn-notebooks | notebook | skipped |" in table
    assert "| score part removed |" in table
    assert "Judged by: the test author" in table


class _NoNetwork:
    """A connector that fails the test if the recording is not enough."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def call_many(self, *args: Any, **kwargs: Any) -> list[CallOutcome]:
        raise AssertionError("a replay must not call upstream")


@pytestmark_async
async def test_a_recording_replays_to_the_same_report_without_the_network(
    ranked_connector: Connector, tmp_path: Path
) -> None:
    golden = golden_from_mapping(GOLDEN)
    recorder = Recorder(ranked_connector)
    live = await evaluate(golden, recorder, now=recorder.now)
    path = tmp_path / "run.json.gz"
    recorder.save(path)

    replayed = Recorder.load(path, _NoNetwork(ranked_connector.settings))  # type: ignore[arg-type]
    assert replayed.replay_only
    assert replayed.recorded_at == recorder.recorded_at
    again = await evaluate(golden, replayed, now=replayed.now)
    assert again.as_dict() == live.as_dict()


@pytestmark_async
async def test_a_replay_without_the_needed_answers_is_inconclusive(ranked_connector: Connector) -> None:
    empty = Recorder(_NoNetwork(ranked_connector.settings), {"recorded_at": "2026-11-14T00:00:00+00:00"})  # type: ignore[arg-type]
    report = await evaluate(golden_from_mapping(GOLDEN), empty, now=empty.now, ablations=False)
    assert report.scored == []
    assert all(t.verdict == "inconclusive" for t in report.tasks)
    assert "not in the recording" in report.tasks[0].note
    assert report.ranking_ndcg is None
    assert "skipped" in render_markdown(report)


@pytestmark_async
async def test_recorder_keeps_security_answers_too(ranked_connector: Connector) -> None:
    recorder = Recorder(ranked_connector, {"fetches": {"https://example.test/a": [200, {"score": 7}]}})
    assert await recorder.fetch("https://example.test/a") == (200, {"score": 7})
    assert await recorder.fetch("https://example.test/missing") == (0, None)  # replay: never the network
