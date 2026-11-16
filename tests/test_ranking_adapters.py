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
    calls = adapter.enrich_calls(list(found.values()))
    for call in calls:
        if call.label == "details":
            adapter.apply(found, call, outcome(f"hf_details_{kind}.md"))
    previews = json.loads((FIXTURES / "hf_preview.json").read_text(encoding="utf-8"))
    # One candidate at a time, so that every dataset in the fixture gets its sample rows read.
    follow_ups = [call for c in found.values() for call in adapter.follow_up_calls([c])]
    for call in [*calls, *follow_ups]:
        if call.label == "details":
            continue
        if call.label == "preview":
            adapter.apply(found, call, outcome(text=previews[call.targets[0]]))
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


# --- model card eval results ----------------------------------------------------------------------


def test_model_index_records_what_each_number_was_measured_on() -> None:
    front = (
        "model-index:\n- name: demo\n  results:\n  - task:\n      type: automatic-speech-recognition\n"
        "    dataset:\n      name: LibriSpeech (clean)\n      type: librispeech_asr\n      config: clean\n"
        "      split: test\n      args:\n        language: en\n    metrics:\n    - name: Test WER\n"
        "      type: wer\n      value: 3.43\n      source:\n        name: someone else\n"
        "        url: https://example.test\n  - task:\n      type: automatic-speech-recognition\n"
        "    dataset:\n      name: LibriSpeech (other)\n      type: librispeech_asr\n      config: other\n"
        "      split: test\n    metrics:\n    - type: wer\n      value: 7.63\n"
    )
    clean, other = A.read_model_index(front)
    assert clean.benchmark == ("librispeech-asr", "clean", "test", "wer")
    assert clean.benchmark_name == "wer on librispeech_asr clean test"
    assert clean.value == pytest.approx(3.43)
    assert other.benchmark == ("librispeech-asr", "other", "test", "wer")  # a different benchmark
    assert clean.metric == "wer"  # not the name of its "source" block


# --- dataset structure and sample rows (recorded) -------------------------------------------------


def test_hf_dataset_structure_from_the_viewer() -> None:
    found = hugging_face("dataset")
    telco = found["aai510-group1/telco-customer-churn"].structure
    assert telco is not None
    assert telco.splits == {"default/test": 1400.0, "default/train": 4200.0, "default/validation": 1400.0}
    assert telco.total_rows == 7000
    assert telco.has_held_out_split is True
    assert telco.preview_of == ("default", "train")
    assert len(telco.columns) == telco.column_count == 52
    # Sample rows were read: this dataset has empty cells in its first rows.
    assert telco.sample_rows == 12
    assert telco.sample_empty_cells and telco.sample_empty_cells > 0
    assert telco.sample_duplicate_rows == 0

    single = found["d0r1h/customer_churn"].structure
    assert single is not None
    assert single.splits == {"default/train": 37000.0}
    assert single.has_held_out_split is False
    assert "age" in single.columns


def test_hf_datasets_the_hub_cannot_load_are_told_apart_from_a_busy_viewer() -> None:
    found = hugging_face("dataset")
    # "The dataset generation failed": a property of the dataset.
    broken = found["kalhosni/CustomerChurnTelecom"]
    assert "viewer_failed" in broken.flags
    assert broken.structure is not None and broken.structure.total_rows is None  # rows unknown, not zero
    # A timeout while fetching sample rows (as recorded): a property of the moment.
    busy = found["d0r1h/customer_churn"]
    assert "viewer_unavailable" in busy.flags
    assert "viewer_failed" not in busy.flags
    assert busy.structure is not None and busy.structure.sample_rows is None


def test_dataset_viewer_messages() -> None:
    script = "### Configs and Splits\n\n> Could not fetch splits from Dataset Viewer: 501 Not Implemented: runs arbitrary Python code"
    assert A.read_dataset_structure(script) is None
    for busy in (
        "429 Too Many Requests: <html>",
        "502 Bad Gateway",
        "API request failed: The operation was aborted due to timeout",
    ):
        assert A._viewer_is_busy(f"> Could not fetch rows from Dataset Viewer: {busy}")
    assert not A._viewer_is_busy(
        "> Could not fetch rows from Dataset Viewer: 500 Internal Server Error: generation failed"
    )
    assert not A._viewer_is_busy("| default | train | 502 | — | 2 | 1 MB |")


def test_sample_rows_count_empty_cells_and_repeats() -> None:
    preview = (
        "## Dataset Preview\n\n### Features\n\n| # | Column | Type |\n|---:|---|---|\n| 1 | text | {} |\n| 2 | label | {} |\n\n"
        "### Rows\n\n| # | text | label |\n|---:|---|---|\n"
        "| 0 | good film | pos |\n| 1 | bad film | neg |\n| 2 | good film | pos |\n| 3 |  | neg |\n| 4 | a \\| b | null |\n"
    )
    structure = A.DatasetStructure()
    A.read_sample_rows(structure, preview)
    assert structure.sample_rows == 5
    assert structure.sample_duplicate_rows == 1
    assert structure.sample_empty_cells == 2  # one blank cell, one "null"


def test_only_the_leading_datasets_get_sample_rows_read() -> None:
    adapter = A.HuggingFace("dataset")
    many = []
    for n in range(7):
        c = Candidate(source="huggingface", need="dataset", id=f"o/d{n}", url="u")
        c.structure = A.DatasetStructure(splits={"default/train": 10.0}, preview_of=("default", "train"))
        many.append(c)
    many[1].flags.add("viewer_failed")
    calls = adapter.follow_up_calls(many)
    assert [call.targets[0] for call in calls] == ["o/d0", "o/d2", "o/d3"]
    assert calls[0].arguments["operations"] == ["dataset_preview"]
    assert A.HuggingFace("model").follow_up_calls(many) == []


def test_kaggle_file_summary() -> None:
    adapter = A.KaggleDatasets()
    c = adapter._new("blastchar/telco-customer-churn", "u")
    _, files = adapter.enrich_calls([c])
    assert files.tool == "get_dataset_files_summary"
    summary = json.loads((FIXTURES / "kaggle_files_summary.json").read_text(encoding="utf-8"))[c.id]
    adapter.apply({c.id: c}, files, outcome(text=json.dumps(summary)))
    assert c.structure is not None
    assert c.structure.file_types == [".csv"]
    assert c.structure.column_count == 21
    assert not c.enriched  # the description is a separate call


# --- browsing a Hub task --------------------------------------------------------------------------


def test_a_phrasing_that_names_a_hub_task_browses_it_instead_of_searching_names() -> None:
    assert A.hub_task("Sentence similarity") == "sentence-similarity"
    assert A.hub_task("automatic_speech_recognition") == "automatic-speech-recognition"
    assert A.hub_task("sentence embeddings") is None

    calls = A.HuggingFace("model").search_calls(["german ner", "token classification"], 10, ["de"])
    by_label = {c.label: c.arguments for c in calls}
    assert list(by_label) == [
        "german ner",
        "german ner (most downloads)",
        "task token-classification (most downloads)",
    ]
    browse = by_label["task token-classification (most downloads)"]
    assert "query" not in browse  # the standard models rarely carry the task in their name
    assert browse["filters"] == ["token-classification", "de"]
    assert browse["sort"] == "downloads"  # never "trending": that fills the list with this week's uploads
    assert [c.baseline for c in calls] == [True, False, False]  # the baseline stays the first name search

    (dataset_browse,) = A.HuggingFace("dataset").search_calls(["question answering"], 5, None)
    assert dataset_browse.arguments["filters"] == ["task_categories:question-answering"]
    assert dataset_browse.baseline
    # Other platforms take the hint and ignore it.
    assert A.KaggleDatasets().search_calls(["question answering"], 5, ["en"])[0].tool == "search_datasets"


def test_github_readme_under_another_name_gets_a_second_try() -> None:
    adapter = A.GitHubCode()
    c = Candidate(source="github", need="code", id="aio-libs/aiohttp", url="u")
    readme = adapter.enrich_calls([c])[0]
    adapter.apply({c.id: c}, readme, outcome(error="failed to get file contents: 404 Not Found"))
    assert c.read_notes == [A.README_MISSING]
    assert not c.enriched

    (retry,) = adapter.follow_up_calls([c])
    assert retry.arguments == {"owner": "aio-libs", "repo": "aiohttp", "path": "README.rst"}
    adapter.apply(
        {c.id: c}, retry, outcome(text="successfully downloaded text file (SHA: 1)\nAsync HTTP client")
    )
    assert c.doc_text == "Async HTTP client"
    assert c.enriched
    assert c.read_notes == []  # found after all
    assert adapter.follow_up_calls([c]) == []  # nothing left to retry

    still_missing = Candidate(source="github", need="code", id="x/y", url="u")
    adapter.apply({"x/y": still_missing}, readme_for(adapter, still_missing), outcome(error="404"))
    (again,) = adapter.follow_up_calls([still_missing])
    adapter.apply({"x/y": still_missing}, again, outcome(error="404"))
    assert still_missing.read_notes == [A.README_MISSING]  # said once


def readme_for(adapter: A.GitHubCode, candidate: Candidate) -> Call:
    return adapter.enrich_calls([candidate])[0]
