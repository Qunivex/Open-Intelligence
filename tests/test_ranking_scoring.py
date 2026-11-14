from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from open_intelligence.config import RankingSettings
from open_intelligence.ranking.models import Candidate, Constraints, EvalResult
from open_intelligence.ranking.scoring import (
    NEUTRAL,
    Scored,
    inflation,
    pick_diverse,
    score_group,
    similarity,
)

NOW = datetime(2026, 11, 13, tzinfo=UTC)
SETTINGS = RankingSettings()


def months_ago(months: float) -> datetime:
    return NOW - timedelta(days=months * 30.44)


def make(
    need: str = "model", source: str = "huggingface", id: str = "owner/name", **fields: Any
) -> Candidate:
    return Candidate(source=source, need=need, id=id, url=f"https://example.test/{id}", **fields)  # type: ignore[arg-type]


def score(
    candidates: list[Candidate],
    phrases: list[str] | None = None,
    must_have: bool = False,
    constraints: Constraints | None = None,
    final: bool = True,
    settings: RankingSettings = SETTINGS,
) -> dict[str, Scored]:
    scored = score_group(
        candidates, phrases or [], must_have, constraints or Constraints(), settings, NOW, final
    )
    return {s.candidate.id: s for s in scored}


def gates(candidate: Candidate, **kwargs: Any) -> list[str]:
    return [gate for gate, _ in score([candidate], **kwargs)[candidate.id].gate_failures]


# --- unknown is neutral ---------------------------------------------------------------------------


def test_a_candidate_with_no_evidence_scores_neutral_and_says_what_is_missing() -> None:
    s = score([make(need="dataset")])["owner/name"]
    assert s.total == pytest.approx(NEUTRAL)
    assert [s.parts[p].value for p in ("quality", "health", "trust", "adoption")] == [None] * 4
    assert "last update date is not known" in s.unresolved
    assert any("license was not reported" in note for note in s.unresolved)
    assert s.gate_failures == []


def test_weights_decide_the_total() -> None:
    only_adoption = RankingSettings(
        weights={"fit": 0, "quality": 0, "health": 0, "trust": 0, "adoption": 1.0}
    )
    low = make(id="a/low", metrics={"downloads": 10.0, "likes": 1.0})
    high = make(id="a/high", metrics={"downloads": 1000.0, "likes": 50.0})
    scored = score([low, high], settings=only_adoption)
    assert scored["a/high"].total == pytest.approx(0.75)
    assert scored["a/low"].total == pytest.approx(0.25)


# --- gates ----------------------------------------------------------------------------------------


def test_archived_repositories_are_out() -> None:
    assert gates(make(need="code", source="github", flags={"archived"})) == ["archived"]


def test_license_gates_follow_the_constraints() -> None:
    non_commercial = make(license="cc-by-nc-4.0", license_reported=True)
    assert gates(non_commercial) == []
    assert gates(non_commercial, constraints=Constraints(commercial_use=True)) == ["license"]
    assert any("does not allow commercial use" in w for w in score([non_commercial])["owner/name"].warnings)

    gpl = make(need="code", source="github", license="GPL-3.0", license_reported=True)
    assert gates(gpl) == []
    assert gates(gpl, constraints=Constraints(copyleft_ok=False)) == ["license"]


def test_unclear_license_is_penalized_and_flagged_but_not_excluded() -> None:
    clear = make(need="dataset", id="a/clear", license="apache-2.0", license_reported=True)
    unclear = make(
        need="dataset", id="a/unclear", license="Data files © Original Authors", license_reported=True
    )
    scored = score([clear, unclear])
    assert scored["a/unclear"].gate_failures == []
    assert scored["a/clear"].parts["trust"].value == pytest.approx(1.0)
    assert scored["a/unclear"].parts["trust"].value == pytest.approx(0.3)
    assert any("confirm the terms before use" in note for note in scored["a/unclear"].unresolved)


def test_pickle_only_weights_are_gated_once_the_files_confirm_it() -> None:
    confirmed = make(weight_formats={"pickle"})
    assert gates(confirmed) == ["weights"]
    assert gates(confirmed, constraints=Constraints(allow_pickle_weights=True)) == []
    from_tags = make(weight_formats={"pickle"}, flags={"weights_from_tags"})
    assert gates(from_tags) == []  # a hint is not enough to exclude
    assert any("pickle" in w for w in score([from_tags])["owner/name"].warnings)
    assert gates(make(weight_formats={"pickle", "safetensors"})) == []


def test_size_and_age_limits() -> None:
    big = make(size_bytes=int(9e9))
    assert gates(big, constraints=Constraints(max_model_size_gb=4)) == ["size"]
    assert gates(big, constraints=Constraints(max_model_size_gb=16)) == []
    unknown = score([make()], constraints=Constraints(max_model_size_gb=4))["owner/name"]
    assert unknown.gate_failures == []
    assert any("size limit was not checked" in note for note in unknown.unresolved)

    old = make(updated_at=months_ago(40))
    assert gates(old, constraints=Constraints(max_age_months=24)) == ["age"]
    assert gates(old) == []


def test_fit_gate_applies_only_after_reading() -> None:
    off_topic = make(title="image upscaler", description="super resolution for photos")
    assert gates(off_topic, phrases=["sentiment analysis"], final=False) == []
    assert gates(off_topic, phrases=["sentiment analysis"], final=True) == ["fit"]


def test_a_pick_must_cover_at_least_one_must_have() -> None:
    partial = make(description="snowflake census loader")  # half of each phrase, none covered
    s = score([partial], phrases=["snowflake scheduling", "census airflow"], must_have=True)["owner/name"]
    assert s.covered == []
    assert s.gate_failures == [("fit", "covers none of the must-haves")]
    good = make(description="snowflake census loader with scheduling")
    s = score([good], phrases=["snowflake scheduling", "census airflow"], must_have=True)["owner/name"]
    assert s.covered == ["snowflake scheduling"]
    assert s.missing == ["census airflow"]
    assert s.gate_failures == []


# --- fit ------------------------------------------------------------------------------------------


def test_programming_language_constraint_changes_fit() -> None:
    python = make(
        need="code", source="github", id="a/py", description="census loader", programming_language="Python"
    )
    go = make(need="code", source="github", id="a/go", description="census loader", programming_language="Go")
    scored = score(
        [python, go], phrases=["census loader"], constraints=Constraints(programming_language="python")
    )
    assert scored["a/py"].parts["fit"].value == pytest.approx(1.0)
    assert scored["a/go"].parts["fit"].value == pytest.approx(0.5)
    assert "written in Go, not python" in scored["a/go"].parts["fit"].reasons


def test_natural_language_constraint_changes_fit() -> None:
    german = make(id="a/de", description="sentiment", languages=["de"])
    english = make(id="a/en", description="sentiment", languages=["en"])
    multi = make(id="a/multi", description="sentiment", languages=["multilingual"])
    unstated = make(id="a/none", description="sentiment")
    scored = score(
        [german, english, multi, unstated], phrases=["sentiment"], constraints=Constraints(languages=["de"])
    )
    fit = {k: v.parts["fit"].value for k, v in scored.items()}
    assert fit == pytest.approx({"a/de": 1.0, "a/multi": 0.9, "a/none": 0.8, "a/en": 0.4})


def test_alternative_phrasings_use_the_best_match_and_must_haves_the_average() -> None:
    c = make(description="named entity recognition for german text")
    alternatives = score([c], phrases=["germeval", "german ner", "named entity recognition"])["owner/name"]
    assert alternatives.parts["fit"].value == pytest.approx(1.0)
    required = score([c], phrases=["named entity recognition", "medical"], must_have=True)["owner/name"]
    assert required.parts["fit"].value == pytest.approx(0.5)


# --- quality, health ------------------------------------------------------------------------------


def test_model_eval_evidence_ranks_verified_over_self_reported_over_none() -> None:
    card = "## How to use\nExample code.\n" + "word " * 200
    verified = make(
        id="a/verified", enriched=True, doc_text=card, evals=[EvalResult("glue", "accuracy", 0.9, True)]
    )
    reported = make(
        id="a/reported", enriched=True, doc_text=card, evals=[EvalResult("glue", "accuracy", 0.9, False)]
    )
    silent = make(id="a/silent", enriched=True, doc_text=card)
    quality = {k: v.parts["quality"].value for k, v in score([verified, reported, silent]).items()}
    assert quality["a/verified"] > quality["a/reported"] > quality["a/silent"]
    assert "no evaluation results in the card" in score([silent])["a/silent"].parts["quality"].reasons


def test_a_card_that_was_cut_off_is_unknown_not_empty() -> None:
    empty = make(need="dataset", id="a/empty", enriched=True, tags=["license:mit"])
    cut_off = make(need="dataset", id="a/cut", enriched=True, tags=["license:mit"], flags={"card_cut_off"})
    scored = score([empty, cut_off])
    assert "dataset card is empty" in scored["a/empty"].parts["quality"].reasons
    assert "dataset card is empty" not in scored["a/cut"].parts["quality"].reasons
    assert scored["a/cut"].parts["quality"].value > scored["a/empty"].parts["quality"].value


def test_freshness_halves_at_the_half_life_and_data_ages_slower_than_code() -> None:
    code = make(need="code", source="github", updated_at=months_ago(18))
    dataset = make(need="dataset", updated_at=months_ago(18))
    assert score([code])["owner/name"].parts["health"].value == pytest.approx(0.5, abs=0.01)
    assert score([dataset])["owner/name"].parts["health"].value > 0.75
    fresh = make(updated_at=NOW)
    assert score([fresh])["owner/name"].parts["health"].value == pytest.approx(1.0)


# --- adoption -------------------------------------------------------------------------------------


def test_adoption_is_a_percentile_among_the_candidates_of_one_source() -> None:
    models = [make(id=f"a/m{n}", metrics={"downloads": float(n), "likes": float(n)}) for n in (10, 100, 1000)]
    kaggle = make(source="kaggle", id="k/only", metrics={"votes": 3.0})
    scored = score([*models, kaggle])
    assert [scored[f"a/m{n}"].parts["adoption"].value for n in (10, 100, 1000)] == pytest.approx(
        [1 / 6, 0.5, 5 / 6]
    )
    # A niche task's leader scores as well as a popular task's leader.
    assert scored["k/only"].parts["adoption"].value == pytest.approx(0.5)


def test_inflated_popularity_is_damped_and_flagged() -> None:
    honest = make(id="a/honest", metrics={"downloads": 2.4e6, "likes": 857.0})
    automated = make(id="a/automated", metrics={"downloads": 3.4e6, "likes": 13.0})
    assert inflation(honest) is None
    assert "look automated" in (inflation(automated) or "")
    scored = score([honest, automated])
    # Without the check the more-downloaded one would score higher.
    assert scored["a/automated"].parts["adoption"].value < scored["a/honest"].parts["adoption"].value
    assert any("look automated" in w for w in scored["a/automated"].warnings)

    assert "stars may be inflated" in (
        inflation(make(need="code", source="github", metrics={"stars": 5000.0, "forks": 12.0})) or ""
    )
    assert inflation(make(need="code", source="github", metrics={"stars": 420.0, "forks": 61.0})) is None
    quiet = make(
        need="dataset", source="kaggle", metrics={"downloads": 50_000.0, "votes": 2.0, "notebooks": 1.0}
    )
    assert "look automated" in (inflation(quiet) or "")


# --- diversity ------------------------------------------------------------------------------------


def test_conversions_and_mirrors_count_as_the_same_option() -> None:
    original = make(id="lab/model-a")
    quantized = make(id="someone/model-a-onnx", base_model="lab/model-a", lineage="quantized")
    finetune_1 = make(id="x/ft-one", base_model="lab/base", lineage="finetune")
    finetune_2 = make(id="y/ft-two", base_model="lab/base", lineage="finetune")
    mirror = make(need="dataset", id="other/imdb-reviews")
    source = make(need="dataset", id="stanford/imdb-reviews")
    assert similarity(original, quantized) == 1.0
    assert similarity(finetune_1, finetune_2) == 0.5
    assert similarity(mirror, source) == 0.9
    assert similarity(original, finetune_1) < 0.5


def test_top_picks_fold_duplicates_into_variants() -> None:
    def scored(c: Candidate, total: float) -> Scored:
        return Scored(candidate=c, total=total)

    ranked = [
        scored(make(id="lab/model-a"), 0.9),
        scored(make(id="someone/model-a-onnx", base_model="lab/model-a", lineage="quantized"), 0.85),
        scored(make(id="lab/model-b"), 0.8),
        scored(make(id="lab/model-c"), 0.7),
        scored(make(id="lab/model-d"), 0.6),
    ]
    picks = pick_diverse(ranked, 3)
    assert [p.candidate.id for p in picks] == ["lab/model-a", "lab/model-b", "lab/model-c"]
    assert picks[0].variants == ["someone/model-a-onnx"]
