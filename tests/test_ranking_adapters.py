"""Parsers against recorded output of the real Hugging Face and Kaggle servers (tests/fixtures).

The GitHub fixtures are hand-written to the documented shapes: see fixtures/README.md.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from open_intelligence.connector import CallOutcome
from open_intelligence.ranking import adapters as A
from open_intelligence.ranking.models import Call, Candidate

FIXTURES = Path(__file__).with_name("fixtures")


def outcome(
    name: str = "", *, text: str | None = None, structured: Any = None, error: str | None = None
) -> CallOutcome:
    body = text if text is not None else ((FIXTURES / name).read_text(encoding="utf-8") if name else "")
    return CallOutcome(
        source="x", tool="t", arguments={}, ok=error is None, text=body, structured=structured, error=error
    )


def hugging_face(kind: str) -> dict[str, Candidate]:
    """Search results of both sorts, then enriched the way the pipeline does it, from the recordings."""
    adapter = A.HuggingFace("model" if kind == "model" else "dataset")
    found: dict[str, Candidate] = {}
    for name in (f"hf_search_{kind}.md", f"hf_search_{kind}_downloads.md"):
        for c in adapter.parse_search(outcome(name), 10):
            found.setdefault(c.id, c)
    recorded = json.loads((FIXTURES / "hf_fs.json").read_text(encoding="utf-8"))
    for call in adapter.enrich_calls(list(found.values())):
        if call.label == "details":
            adapter.apply(found, call, outcome(f"hf_details_{kind}.md"))
            continue
        results = []
        for index, op in enumerate(call.arguments["operations"]):
            entry = recorded.get(f"{op['cmd']} {op['args'][0]}")
            results.append(
                {"index": index, **(entry or {"status": "error", "error": {"code": "X", "message": "m"}})}
            )
        adapter.apply(found, call, outcome(structured={"results": results}))
    return found


# --- small readers --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "number"),
    [("4.7K", 4700.0), ("121.6K", 121600.0), ("3.4M", 3.4e6), ("52", 52.0), ("1,234", 1234.0), (17, 17.0)],
)
def test_parse_number(text: Any, number: float) -> None:
    assert A.parse_number(text) == pytest.approx(number)


@pytest.mark.parametrize("text", ["", "n/a", None, True, "12 apples"])
def test_parse_number_unknown(text: Any) -> None:
    assert A.parse_number(text) is None


def test_parse_date() -> None:
    assert A.parse_date("2018-02-23T18:20:00.210Z") == datetime(2018, 2, 23, 18, 20, 0, 210000, tzinfo=UTC)
    assert A.parse_date("2024-10-19T09:57:24.841578800Z") is not None  # nine fraction digits (Kaggle)
    assert A.parse_date("12 Nov, 2023") == datetime(2023, 11, 12, tzinfo=UTC)  # Hugging Face
    assert A.parse_date("soon") is None
    assert A.parse_date(None) is None


def test_front_matter_and_model_index() -> None:
    readme = (
        "---\nlanguage: en\nlicense: apache-2.0\ndatasets:\n- sst2\n- glue\ntags: [sentiment, 'text']\n"
        "model-index:\n- name: demo\n  results:\n  - task:\n      type: text-classification\n    dataset:\n"
        "      name: glue\n      type: glue\n    metrics:\n    - type: accuracy\n      value: 0.91\n"
        "      name: Accuracy\n      verified: true\n    - name: F1 score\n      type: f1\n      value: 0.9\n"
        "---\n# Title\nBody text.\n"
    )
    front, body, complete = A.split_front_matter(readme)
    assert complete
    assert body.startswith("# Title")
    meta = A.read_front_matter(front)
    assert meta["license"] == ["apache-2.0"]
    assert meta["datasets"] == ["sst2", "glue"]
    assert meta["tags"] == ["sentiment", "text"]
    evals = A.read_model_index(front)
    assert [(e.dataset, e.metric, e.value, e.verified) for e in evals] == [
        ("glue", "accuracy", 0.91, True),
        ("glue", "f1", 0.9, False),
    ]
    assert evals[0].describe() == "accuracy 0.91 on glue (verified by Hugging Face)"


def test_front_matter_cut_off_is_reported_as_incomplete() -> None:
    front, body, complete = A.split_front_matter("---\nlicense: mit\ndataset_info:\n  features:\n  - name: a")
    assert not complete
    assert body == ""
    assert A.read_front_matter(front)["license"] == ["mit"]
    assert A.split_front_matter("# No front matter\ntext") == ("", "# No front matter\ntext", True)


# --- Hugging Face (recorded) ----------------------------------------------------------------------


def test_hf_model_search_results() -> None:
    found = A.HuggingFace("model").parse_search(outcome("hf_search_model.md"), 10)
    by_id = {c.id: c for c in found}
    assert [c.id for c in found][:2] == [
        "Xenova/distilroberta-finetuned-financial-news-sentiment-analysis",
        "kinit/slovakbert-sentiment-twitter",
    ]
    original = by_id["mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"]
    assert original.license == "apache-2.0"
    assert original.task == "text-classification"
    assert original.library == "transformers"
    assert original.metrics["downloads"] == pytest.approx(121_600)
    assert original.metrics["likes"] == 480
    assert "model-index" in original.flags
    assert (
        original.url
        == "https://huggingface.co/mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"
    )
    conversion = by_id["Xenova/distilroberta-finetuned-financial-news-sentiment-analysis"]
    assert conversion.lineage == "quantized"
    assert conversion.base_model == original.id
    slovak = by_id["kinit/slovakbert-sentiment-twitter"]
    assert slovak.languages == ["sk"]
    assert slovak.weight_formats == {"pickle"}
    assert "weights_from_tags" in slovak.flags  # a hint until the files are listed


def test_hf_search_respects_the_limit_and_survives_garbage() -> None:
    adapter = A.HuggingFace("model")
    assert len(adapter.parse_search(outcome("hf_search_model.md"), 2)) == 2
    assert adapter.parse_search(outcome(text="No results. ### not a repo id\n"), 5) == []
    assert adapter.parse_search(outcome(text=""), 5) == []


def test_hf_models_after_reading_the_shortlist() -> None:
    found = hugging_face("model")
    slovak = found["kinit/slovakbert-sentiment-twitter"]
    assert slovak.enriched
    assert slovak.weight_formats == {"pickle"}
    assert "weights_from_tags" not in slovak.flags  # confirmed by the file list
    assert A.pickle_only(slovak) is True
    assert slovak.size_bytes == 498_673_997
    assert "Sentiment Analysis model based on SlovakBERT" in slovak.doc_text

    original = found["mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"]
    assert original.params == pytest.approx(82.1e6)
    assert original.metrics["spaces"] == 100
    assert original.metrics["downloads_all_time"] > original.metrics["downloads"]
    assert A.has_safe_weights(original) is True
    assert A.pickle_only(original) is False
    first = original.evals[0]
    assert (first.dataset, first.metric, first.verified) == ("financial_phrasebank", "accuracy", False)
    assert first.value == pytest.approx(0.9823, abs=1e-3)

    latest = found["cardiffnlp/twitter-roberta-base-sentiment-latest"]
    assert latest.updated_at == datetime(2025, 8, 4, tzinfo=UTC)
    assert latest.weight_formats == {"pickle", "h5"}
    assert A.has_safe_weights(latest) is False
    assert A.pickle_only(latest) is False

    conversion = found["Xenova/distilroberta-finetuned-financial-news-sentiment-analysis"]
    assert conversion.license is None
    assert conversion.license_reported  # the card was read: no license is declared


def test_hf_datasets_after_reading_the_shortlist() -> None:
    found = hugging_face("dataset")
    assert found["d0r1h/customer_churn"].license == "apache-2.0"
    assert found["d0r1h/customer_churn"].url == "https://huggingface.co/datasets/d0r1h/customer_churn"
    unreadable = found["krisnadwipaj/customer-churn"]
    assert not unreadable.enriched
    assert unreadable.read_notes == ["card could not be read"]
    long_metadata = found["aai510-group1/telco-customer-churn"]
    assert "card_cut_off" in long_metadata.flags
    assert long_metadata.doc_text  # falls back to the description from the details page
    assert long_metadata.languages == ["en"]


# --- Kaggle (recorded) ----------------------------------------------------------------------------


def test_kaggle_datasets() -> None:
    adapter = A.KaggleDatasets()
    found = adapter.parse_search(outcome("kaggle_search_datasets.json"), 10)
    assert len(found) == 5
    assert (
        len(adapter.parse_search(outcome("kaggle_search_datasets.json"), 2)) == 2
    )  # Kaggle ignores pageSize
    telco = found[0]
    assert telco.id == "blastchar/telco-customer-churn"
    assert telco.license == "Data files © Original Authors"
    assert telco.metrics["notebooks"] == 2995
    assert telco.metrics["usability"] == pytest.approx(0.88, abs=0.01)
    assert telco.updated_at == datetime(2018, 2, 23, 18, 20, 0, 210000, tzinfo=UTC)
    assert not telco.enriched

    info = json.loads((FIXTURES / "kaggle_dataset_info.json").read_text(encoding="utf-8"))[telco.id]
    call = adapter.enrich_calls([telco])[0]
    assert call.arguments == {"request": {"ownerSlug": "blastchar", "datasetSlug": "telco-customer-churn"}}
    adapter.apply({telco.id: telco}, call, outcome(text=json.dumps(info)))
    assert telco.enriched
    assert "customer" in telco.doc_text.lower()

    other = found[1]
    adapter.apply(
        {other.id: other}, adapter.enrich_calls([other])[0], outcome(error="kaggle returned an error")
    )
    assert not other.enriched
    assert other.read_notes == ["dataset description could not be read"]


def test_kaggle_models() -> None:
    found = A.KaggleModels().parse_search(outcome("kaggle_list_models.json"), 10)
    by_id = {c.id: c for c in found}
    assert len(found) == 4
    first = by_id["sujitmadhavan/sentiment"]
    assert first.license == "MIT"
    assert "<=" in first.description  # HTML entities are unescaped
    assert first.enriched  # list_models already carries the card
    assert by_id["gpreda/gemma2-sentiment-analysis"].library == "transformers"
    assert by_id["alfathterry/distilbert-amazon-review-positive-negative"].library == "tensorflow2"
    assert A.pickle_only(first) is None  # Kaggle does not say which weight formats a model ships


def test_kaggle_notebooks_field_names_are_tolerant() -> None:
    text = json.dumps(
        {
            "kernels": [
                {"ref": "someone/churn-eda", "title": "Churn EDA", "total_votes": 12, "language": "python"}
            ]
        }
    )
    found = A.KaggleNotebooks().parse_search(outcome(text=text), 5)
    assert found[0].metrics["votes"] == 12
    assert found[0].url == "https://www.kaggle.com/code/someone/churn-eda"


# --- GitHub (documented shapes, synthetic) --------------------------------------------------------


def test_github_search_results() -> None:
    found = A.GitHubCode().parse_search(outcome("github_search_repositories.json"), 10)
    by_id = {c.id: c for c in found}
    assert len(found) == 5
    acme = by_id["example-acme/census-snowflake-sync"]
    assert acme.license == "MIT"
    assert acme.programming_language == "Python"
    assert acme.metrics["stars"] == 420
    assert acme.updated_at == datetime(2026, 10, 20, 8, tzinfo=UTC)  # pushed_at, not updated_at
    assert "etl" in acme.tags
    assert "archived" in by_id["example-oldco/snowflake-census-loader"].flags
    assert "fork" in by_id["example-forky/census-snowflake-sync"].flags
    unlicensed = by_id["example-hype/snowflake-pipeline-pro"]
    assert unlicensed.license is None
    assert unlicensed.license_reported  # GitHub said: no license


def test_github_minimal_output_leaves_the_license_unknown() -> None:
    text = json.dumps(
        {
            "items": [
                {
                    "full_name": "a/b",
                    "html_url": "https://github.com/a/b",
                    "stargazers_count": 5,
                    "archived": False,
                }
            ]
        }
    )
    (c,) = A.GitHubCode().parse_search(outcome(text=text), 5)
    assert c.license is None
    assert not c.license_reported  # not reported is different from "no license"


def test_github_search_asks_for_full_objects_and_never_ranks_by_stars_alone() -> None:
    calls = A.GitHubCode().search_calls(["census snowflake", "reverse etl"], 10)
    assert [c.arguments.get("sort") for c in calls] == [None, "stars", None]
    assert all(c.arguments["minimal_output"] is False for c in calls)
    assert [c.baseline for c in calls] == [True, False, False]


def test_github_readme_and_release() -> None:
    adapter = A.GitHubCode()
    c = Candidate(source="github", need="code", id="a/b", url="u")
    readme, release = adapter.enrich_calls([c])
    assert readme.arguments == {"owner": "a", "repo": "b", "path": "README.md"}
    adapter.apply(
        {"a/b": c}, readme, outcome(text="successfully downloaded text file (SHA: 1)\n# Title\nHello")
    )
    assert c.doc_text == "# Title\nHello"
    assert c.enriched
    adapter.apply(
        {"a/b": c}, release, outcome(text='{"tag_name": "v1", "published_at": "2026-01-02T00:00:00Z"}')
    )
    assert c.has_releases is True
    assert c.released_at == datetime(2026, 1, 2, tzinfo=UTC)

    none = Candidate(source="github", need="code", id="c/d", url="u")
    adapter.apply(
        {"c/d": none},
        Call("get_latest_release", {}, "release", targets=["c/d"]),
        outcome(error="404 Not Found"),
    )
    assert none.has_releases is False
    unknown = Candidate(source="github", need="code", id="e/f", url="u")
    adapter.apply(
        {"e/f": unknown}, Call("get_latest_release", {}, "release", targets=["e/f"]), outcome(error="timeout")
    )
    assert unknown.has_releases is None
    assert unknown.read_notes == ["latest release could not be read"]
