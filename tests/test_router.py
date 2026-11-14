from __future__ import annotations

import pytest

from open_intelligence.router import route


@pytest.mark.parametrize(
    ("task", "needs", "sources"),
    [
        # The motivating example: data engineering, no ML. Data flowing through is not a dataset need.
        (
            "I am building a python project to automate a Snowflake pipeline to send data from Census to Snowflake",
            ["code"],
            ["github"],
        ),
        ("Build a reverse ETL sync from our warehouse to Salesforce", ["code"], ["github"]),
        ("Write dbt data models for our Snowflake marts", ["code"], ["github"]),
        ("Add a Django model and REST API for orders", ["code"], ["github"]),
        (
            "Fine-tune a sentiment classifier on product reviews",
            ["code", "model", "dataset"],
            ["github", "huggingface", "kaggle"],
        ),
        (
            "Train a model to detect defects in PCB photos",
            ["code", "model", "dataset"],
            ["github", "huggingface", "kaggle"],
        ),
        (
            "Build a RAG chatbot over our internal docs",
            ["code", "model"],
            ["github", "huggingface", "kaggle"],
        ),
        (
            "Which open-source embedding model is best for semantic search in German?",
            ["model"],
            ["huggingface", "kaggle"],
        ),
        ("Find a dataset of labeled chest x-ray images", ["dataset"], ["huggingface", "kaggle"]),
        ("Predict customer churn from our CRM export", ["code", "notebook"], ["github", "kaggle"]),
        ("Transcribe meeting recordings and summarize them", ["model"], ["huggingface", "kaggle"]),
        ("Build a CLI tool to resize images, do not search kaggle", ["code"], ["github"]),
    ],
)
def test_routes_by_artifact_kind(task: str, needs: list[str], sources: list[str]) -> None:
    decision = route(task)
    assert decision.need_names == needs
    assert decision.source_ids == sources
    assert decision.decided_by == "inferred"


def test_snowflake_example_skips_model_hubs_with_reasons() -> None:
    decision = route(
        "Python project to automate a Snowflake pipeline that sends data from Census to Snowflake"
    )
    assert decision.confidence == "high"
    skipped = {s["source"]: s["why"] for s in decision.skipped}
    assert set(skipped) == {"huggingface", "kaggle"}
    assert "no model or dataset need" in skipped["huggingface"]
    code = decision.needs[0]
    assert {"python", "automate", "pipeline"} <= set(code.evidence)


def test_model_hub_order_is_huggingface_primary_kaggle_secondary() -> None:
    decision = route("Fine-tune a sentiment classifier on product reviews")
    roles = {s.source: s.role for s in decision.sources}
    assert roles == {"github": "primary", "huggingface": "primary", "kaggle": "secondary"}
    implied = {n.need: n.implied_by for n in decision.needs}
    assert implied["dataset"] and "training" in implied["dataset"]
    assert implied["code"] and "training" in implied["code"]


def test_non_ml_model_phrases_are_noted_not_routed() -> None:
    decision = route("I need a pricing model for my SaaS")
    assert "model" not in decision.need_names
    assert any("pricing model" in n for n in decision.notes)


def test_bare_model_without_ml_context_is_an_ambiguity() -> None:
    decision = route("Generate the model files for the user service")
    assert "model" not in decision.need_names
    assert decision.confidence == "medium"
    assert any("without any ML context" in a for a in decision.ambiguities)


def test_single_weak_ml_word_is_not_enough() -> None:
    decision = route("Weather forecast app in React")
    assert decision.source_ids == ["github"]
    assert decision.confidence == "medium"
    assert any("'forecast'" in a for a in decision.ambiguities)


def test_type_inference_is_not_ml() -> None:
    assert route("Improve type inference in our TypeScript compiler plugin").source_ids == ["github"]


def test_no_signal_defaults_to_code_with_low_confidence() -> None:
    decision = route("something cool")
    assert decision.need_names == ["code"]
    assert decision.confidence == "low"


def test_naming_a_platform_adds_it() -> None:
    decision = route("anything good on Hugging Face for this?")
    assert "huggingface" in decision.source_ids
    assert set(decision.need_names) >= {"model", "dataset"}


@pytest.mark.parametrize(
    "phrase", ["without kaggle", "not on Hugging Face", "skip GitHub", "don't use kaggle"]
)
def test_exclusions_are_respected(phrase: str) -> None:
    decision = route(f"Fine-tune a sentiment classifier, {phrase}")
    excluded = {
        "without kaggle": "kaggle",
        "not on Hugging Face": "huggingface",
        "skip GitHub": "github",
        "don't use kaggle": "kaggle",
    }[phrase]
    assert excluded not in decision.source_ids
    assert any(s["source"] == excluded and "excluded" in s["why"] for s in decision.skipped)


def test_caller_needs_win_and_disagreements_are_reported() -> None:
    decision = route("Fine-tune a sentiment classifier on product reviews", needs=["code"])
    assert decision.decided_by == "caller"
    assert decision.source_ids == ["github"]
    assert decision.confidence == "high"
    assert any("'model'" in a for a in decision.ambiguities)
    assert any("'dataset'" in a for a in decision.ambiguities)


def test_caller_needs_are_validated() -> None:
    with pytest.raises(ValueError, match="unknown needs"):
        route("x", needs=["weights"])


def test_decision_serializes() -> None:
    data = route("Find a German NER dataset").as_dict()
    assert data["needs"][0]["need"] == "dataset"
    assert data["needs"][0]["meaning"]
    assert {s["source"] for s in data["sources"]} == {"huggingface", "kaggle"}
