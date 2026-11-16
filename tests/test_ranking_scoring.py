from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from open_intelligence.config import RankingSettings
from open_intelligence.ranking.models import (
    Advisory,
    Candidate,
    Constraints,
    DatasetStructure,
    EvalResult,
    SecurityEvidence,
)
from open_intelligence.ranking.scoring import (
    NEUTRAL,
    Scored,
    benchmark_standings,
    benchmark_tables,
    fold_copies,
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
    assert scored["a/unclear"].parts["trust"].value == pytest.approx(0.5)
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


# --- benchmark comparison -------------------------------------------------------------------------


def evals(*results: tuple[str, str, str, float]) -> list[EvalResult]:
    return [EvalResult(dataset=d, metric=m, value=v, split=s) for d, s, m, v in results]


def test_models_are_compared_only_on_the_same_benchmark() -> None:
    card = "## How to use\n" + "word " * 200
    small = make(id="a/small", enriched=True, doc_text=card, evals=evals(("librispeech", "test", "wer", 3.4)))
    base = make(id="a/base", enriched=True, doc_text=card, evals=evals(("librispeech", "test", "wer", 5.0)))
    large = make(
        id="a/large", enriched=True, doc_text=card, evals=evals(("librispeech", "test", "wer", 12.3))
    )
    # Same dataset and metric, but measured on the training split: not comparable, so not ranked.
    train = make(
        id="a/train", enriched=True, doc_text=card, evals=evals(("librispeech", "train", "wer", 0.1))
    )
    alone = make(id="a/alone", enriched=True, doc_text=card, evals=evals(("other-set", "test", "wer", 1.0)))

    standings = benchmark_standings([small, base, large, train, alone])
    assert set(standings) == {small.key, base.key, large.key}
    assert [standings[c.key][0].rank for c in (small, base, large)] == [1, 2, 3]  # lower WER is better
    assert standings[small.key][0].describe() == (
        "wer on librispeech test: 3.4, best of the 3 candidates that report it"
    )

    quality = {k: v.parts["quality"].value for k, v in score([small, base, large, train, alone]).items()}
    assert quality["a/small"] > quality["a/base"] > quality["a/large"]
    assert quality["a/train"] == quality["a/alone"] == quality["a/base"]  # no comparison: unchanged
    assert any("best of the 3" in r for r in score([small, base, large])["a/small"].parts["quality"].reasons)


def test_benchmark_tables_put_ratios_and_percentages_on_one_scale() -> None:
    ratio = make(id="a/ratio", evals=evals(("sst2", "validation", "accuracy", 0.91)))
    percent = make(id="a/percent", evals=evals(("sst2", "validation", "accuracy", 93.0)))
    (table,) = benchmark_tables([ratio, percent])
    assert table["benchmark"] == "accuracy on sst2 validation"
    assert table["better"] == "higher"
    assert [r["id"] for r in table["results"]] == ["a/percent", "a/ratio"]  # 93% beats 0.91
    assert benchmark_tables([ratio]) == []


# --- dataset structure ----------------------------------------------------------------------------


def dataset(id: str, structure: DatasetStructure | None = None, **fields: Any) -> Candidate:
    return make(
        need="dataset", id=id, enriched=True, doc_text="A dataset. " * 40, structure=structure, **fields
    )


def test_dataset_structure_checks_feed_quality() -> None:
    solid = dataset(
        "a/solid",
        DatasetStructure(
            splits={"default/train": 25_000.0, "default/test": 25_000.0},
            total_rows=50_000.0,
            columns=["text", "label"],
            label_names=["neg", "pos"],
            sample_rows=30,
            sample_empty_cells=0,
            sample_duplicate_rows=0,
        ),
    )
    single = dataset("a/single", DatasetStructure(splits={"default/train": 37_000.0}, total_rows=37_000.0))
    repeated = dataset(
        "a/repeated",
        DatasetStructure(
            splits={"default/train": 900.0}, total_rows=900.0, sample_rows=30, sample_duplicate_rows=7
        ),
    )
    scored = score([solid, single, repeated])
    quality = {k: v.parts["quality"].value for k, v in scored.items()}
    assert quality["a/solid"] > quality["a/single"] > quality["a/repeated"]
    reasons = scored["a/solid"].parts["quality"].reasons
    assert "splits: train 25.0K, test 25.0K" in reasons
    assert "label classes are named (neg, pos)" in reasons
    assert "no empty cells or repeated rows in the first 30 rows" in reasons
    assert (
        "no test or validation split: hold some data out yourself"
        in scored["a/single"].parts["quality"].reasons
    )
    assert "7 repeated rows among the first 30" in scored["a/repeated"].warnings


def test_dataset_that_cannot_be_loaded_is_flagged_and_a_busy_viewer_is_not_held_against_it() -> None:
    script = dataset("a/script", flags={"loading_script"})
    broken = dataset("a/broken", DatasetStructure(splits={"default/train": None}), flags={"viewer_failed"})
    busy = dataset("a/busy", flags={"viewer_unavailable"})
    plain = dataset("a/plain")
    scored = score([script, broken, busy, plain])
    assert any("Python script" in w for w in scored["a/script"].warnings)
    assert any("cannot load its rows" in w for w in scored["a/broken"].warnings)
    # A rate limit says nothing about the dataset: same score as one never looked at, with a note.
    assert scored["a/busy"].parts["quality"].value == scored["a/plain"].parts["quality"].value
    assert scored["a/busy"].warnings == []
    assert any("did not answer" in note for note in scored["a/busy"].unresolved)
    assert scored["a/script"].parts["quality"].value < scored["a/plain"].parts["quality"].value


def test_dataset_columns_count_towards_fit() -> None:
    plain = dataset("a/plain")
    with_columns = dataset("a/columns", DatasetStructure(columns=["tokens", "ner_tags"]))
    scored = score([plain, with_columns], phrases=["ner tags"])
    assert scored["a/columns"].parts["fit"].value == pytest.approx(1.0)
    assert scored["a/plain"].parts["fit"].value == pytest.approx(0.0)


def test_kaggle_dataset_structure() -> None:
    files = make(
        need="dataset",
        source="kaggle",
        id="k/files",
        structure=DatasetStructure(file_types=[".csv"], column_count=21),
    )
    s = score([files])["k/files"]
    assert "21 columns in .csv files" in s.parts["quality"].reasons
    assert any("not available from Kaggle" in note for note in s.unresolved)


# --- security -------------------------------------------------------------------------------------


def repo(id: str, security: SecurityEvidence | None, **fields: Any) -> Candidate:
    return make(
        need="code", source="github", id=id, license="MIT", license_reported=True, security=security, **fields
    )


def test_scorecard_and_advisories_feed_trust() -> None:
    clean = repo(
        "a/clean",
        SecurityEvidence(
            scorecard=8.4,
            scorecard_date="2026-10-26",
            scorecard_checks={"Maintained": 10},
            package="pypi:clean",
            package_version="2.0.0",
            advisories=[],
        ),
    )
    exposed = repo(
        "a/exposed",
        SecurityEvidence(
            scorecard=3.0,
            scorecard_checks={"Maintained": 0, "Dangerous-Workflow": 0},
            package="pypi:exposed",
            package_version="1.2.0",
            advisories=[Advisory("GHSA-aaaa", "Session cookie disclosure", 7.5)],
        ),
    )
    unknown = repo("a/unknown", SecurityEvidence(notes=["not covered by OpenSSF Scorecard"]))
    not_looked_up = repo("a/skipped", None)
    scored = score([clean, exposed, unknown, not_looked_up])
    trust = {k: v.parts["trust"].value for k, v in scored.items()}
    assert trust["a/clean"] > trust["a/unknown"] == trust["a/skipped"] > trust["a/exposed"]

    assert "OpenSSF Scorecard 8.4/10, scanned 2026-10-26" in scored["a/clean"].parts["trust"].reasons
    assert "no known vulnerabilities affect pypi:clean 2.0.0" in scored["a/clean"].parts["trust"].reasons
    warnings = scored["a/exposed"].warnings
    assert "1 known vulnerabilities affect pypi:exposed 1.2.0: GHSA-aaaa (high)" in warnings
    assert "Scorecard: no maintenance activity in the last 90 days" in warnings
    assert "Scorecard: a dangerous pattern in its CI workflows" in warnings
    assert scored["a/exposed"].gate_failures == []  # high is a warning; critical is a gate
    assert "not covered by OpenSSF Scorecard" in scored["a/unknown"].unresolved
    assert any("were not looked up" in note for note in scored["a/skipped"].unresolved)


def test_a_critical_vulnerability_in_the_current_release_is_a_gate() -> None:
    critical = repo(
        "a/critical",
        SecurityEvidence(
            scorecard=9.0,
            package="npm:x",
            package_version="1.0.0",
            advisories=[Advisory("GHSA-crit", "RCE", 9.8)],
        ),
    )
    assert gates(critical) == ["security"]
    assert score([critical])["a/critical"].gate_failures[0][1].endswith("GHSA-crit")


# --- generic category matches ---------------------------------------------------------------------


def test_matching_only_the_task_category_is_a_weaker_fit_than_matching_a_specific_phrasing() -> None:
    squad = make(
        need="dataset",
        id="a/squad",
        description="reading comprehension",
        tags=["task_categories:question-answering"],
    )
    quiz = make(
        need="dataset",
        id="a/quiz",
        description="multiple choice exam",
        tags=["task_categories:question-answering"],
    )
    specifics = [
        "squad",
        "reading comprehension",
    ]  # "question answering" is the category name, not one of these

    def fits(phrases: list[str], must_have: bool, specific: list[str] | None) -> dict[str, float | None]:
        scored = score_group([squad, quiz], phrases, must_have, Constraints(), SETTINGS, NOW, True, specific)
        return {s.candidate.id: s.parts["fit"].value for s in scored}

    # As a must-have, and as a plain phrasing: the category alone keeps three quarters of the fit.
    assert fits(["question answering"], True, specifics) == pytest.approx({"a/squad": 1.0, "a/quiz": 0.75})
    assert fits(["squad", "reading comprehension", "question answering"], False, specifics) == pytest.approx(
        {"a/squad": 1.0, "a/quiz": 0.75}
    )
    # With nothing specific to compare against, nothing changes.
    assert fits(["question answering"], True, None) == pytest.approx({"a/squad": 1.0, "a/quiz": 1.0})
    scored = score_group([quiz], ["question answering"], True, Constraints(), SETTINGS, NOW, True, specifics)
    assert (
        "matches the task category, but none of the specific search phrasings"
        in scored[0].parts["fit"].reasons
    )


# --- copies of the same thing ---------------------------------------------------------------------


def kaggle_dataset(
    id: str, title: str, year: int, license: str, notebooks: float, size: int = 175_000
) -> Candidate:
    return make(
        need="dataset",
        source="kaggle",
        id=id,
        title=title,
        description="customer churn",
        license=license,
        license_reported=True,
        updated_at=datetime(year, 1, 1, tzinfo=UTC),
        metrics={"notebooks": notebooks, "downloads": notebooks * 200, "votes": notebooks},
        size_bytes=size,
        enriched=True,
    )


def test_the_original_dataset_stands_for_its_copies_even_when_a_copy_scores_higher() -> None:
    original = kaggle_dataset(
        "first/telco-customer-churn", "Telco Customer Churn", 2018, "Data files © Original Authors", 2995
    )
    newer = kaggle_dataset("copy/telco-customer-churn-data", "Telco Customer Churn Data", 2026, "MIT", 18)
    wider = kaggle_dataset(
        "copy/telco-customer-churn-11-1-3", "Telco Customer Churn 11.1.3+", 2024, "MIT", 43, size=260_000
    )
    other = kaggle_dataset(
        "bank/bank-customer-churn", "Bank Customer Churn", 2023, "CC0: Public Domain", 190, size=191_965
    )
    scored = score([original, newer, wider, other], phrases=["customer churn"])
    # The point of the fix: on their own scores the copies win (newer, tidier license label).
    assert scored["copy/telco-customer-churn-data"].total > scored["first/telco-customer-churn"].total

    leaders = fold_copies(list(scored.values()))
    assert {s.candidate.id for s in leaders} == {"first/telco-customer-churn", "bank/bank-customer-churn"}
    telco = next(s for s in leaders if s.candidate.id == "first/telco-customer-churn")
    assert set(telco.variants) == {"copy/telco-customer-churn-data", "copy/telco-customer-churn-11-1-3"}
    assert telco.lead == max(scored[i].total for i in scored if "telco" in i)  # read priority of the family
    assert (
        telco.total == scored["first/telco-customer-churn"].total
    )  # its own evidence, until weights are given
    assert (
        "the original among 3 copies of the same dataset that were found" in telco.parts["adoption"].reasons
    )
    assert any("confirm the terms before use" in note for note in telco.unresolved)


def test_a_generic_name_alone_does_not_make_two_datasets_the_same() -> None:
    one = kaggle_dataset("a/customer-churn", "Customer Churn", 2020, "MIT", 250, size=46_000)
    two = kaggle_dataset(
        "b/customer-churn-dataset", "Customer Churn Dataset", 2023, "MIT", 160, size=3_100_000
    )
    mirror = kaggle_dataset("c/customer-churn", "Customer Churn", 2024, "MIT", 3, size=46_500)
    hub = make(
        need="dataset", id="d/customer_churn", description="customer churn", updated_at=NOW, enriched=True
    )
    leaders = fold_copies(list(score([one, two, mirror, hub], phrases=["customer churn"]).values()))
    by_id = {s.candidate.id: s for s in leaders}
    # Same name and the same size: a copy. Same name, another size or another platform: not shown to be.
    assert set(by_id) == {"a/customer-churn", "b/customer-churn-dataset", "d/customer_churn"}
    assert by_id["a/customer-churn"].variants == ["c/customer-churn"]


def test_a_distinctive_name_links_copies_across_platforms() -> None:
    kaggle = kaggle_dataset("first/telco-customer-churn", "Telco Customer Churn", 2018, "Other", 2995)
    hub = make(
        need="dataset",
        id="group/telco-customer-churn",
        description="customer churn",
        created_at=datetime(2024, 6, 1, tzinfo=UTC),
        metrics={"downloads": 500.0, "likes": 16.0},
        enriched=True,
    )
    (leader,) = fold_copies(list(score([kaggle, hub], phrases=["customer churn"]).values()))
    assert (
        leader.candidate.id == "first/telco-customer-churn"
    )  # equally used on its platform, and there first
    assert leader.variants == ["huggingface:group/telco-customer-churn"]


def test_a_model_stands_for_its_conversions_and_gated_or_unread_members_do_not_represent() -> None:
    base = make(
        id="lab/model", description="sentiment", enriched=True, metrics={"downloads": 900.0, "likes": 40.0}
    )
    onnx = make(
        id="other/model-onnx",
        description="sentiment",
        base_model="lab/model",
        lineage="quantized",
        updated_at=NOW,
        enriched=True,
        metrics={"downloads": 50.0, "likes": 2.0},
    )
    scored = score([base, onnx], phrases=["sentiment"])
    (leader,) = fold_copies(list(scored.values()))
    assert leader.candidate.id == "lab/model"
    assert leader.variants == ["other/model-onnx"]
    # Only what was read can represent a family.
    (leader,) = fold_copies(list(score([base, onnx], phrases=["sentiment"]).values()), prefer={onnx.key})
    assert leader.candidate.id == "other/model-onnx"
    # A member that failed a gate is not part of the family at all.
    gated = score([base, onnx], phrases=["sentiment"])
    gated["lab/model"].gate_failures.append(("weights", "pickle only"))
    (leader,) = fold_copies(list(gated.values()))
    assert leader.candidate.id == "other/model-onnx"
    assert leader.variants == []


def test_a_repository_whose_readme_was_not_read_is_unknown_not_perfect() -> None:
    released = make(need="code", source="github", id="a/released", has_releases=True)
    nothing = make(need="code", source="github", id="a/nothing")
    scored = score([released, nothing])
    assert scored["a/released"].parts["quality"].value == pytest.approx(
        0.65
    )  # releases known, the rest neutral
    assert scored["a/nothing"].parts["quality"].value is None
    assert "README was not read" in scored["a/released"].unresolved


def test_a_dataset_is_judged_on_the_best_evidence_about_the_data_but_keeps_its_own_license() -> None:
    original = kaggle_dataset(
        "first/telco-customer-churn", "Telco Customer Churn", 2018, "Data files © Original Authors", 2995
    )
    newer = kaggle_dataset("copy/telco-customer-churn-data", "Telco Customer Churn Data", 2026, "MIT", 18)
    other = kaggle_dataset(
        "bank/bank-customer-churn", "Bank Customer Churn", 2023, "CC0: Public Domain", 190, size=191_965
    )
    scored = score([original, newer, other], phrases=["customer churn"])
    own_total = scored["first/telco-customer-churn"].total
    own_trust = scored["first/telco-customer-churn"].parts["trust"].value

    leaders = fold_copies(list(scored.values()), weights=SETTINGS.weights)
    telco = next(s for s in leaders if s.candidate.id == "first/telco-customer-churn")
    # Freshness comes from the copy that was kept up; the license stays the original's.
    assert telco.parts["health"].value == scored["copy/telco-customer-churn-data"].parts["health"].value
    assert any("(from its copy copy/telco-customer-churn-data)" in r for r in telco.parts["health"].reasons)
    assert telco.parts["trust"].value == own_trust == pytest.approx(0.5)
    assert telco.parts["adoption"].value == scored["first/telco-customer-churn"].parts["adoption"].value
    assert telco.total > own_total
    # A dataset with no copies is untouched.
    bank = next(s for s in leaders if s.candidate.id == "bank/bank-customer-churn")
    assert bank.total == scored["bank/bank-customer-churn"].total


# --- copies found by content ----------------------------------------------------------------------

TELCO_COLUMNS = [
    "customerID",
    "gender",
    "SeniorCitizen",
    "Partner",
    "Dependents",
    "tenure",
    "Contract",
    "Churn",
]


def hub_dataset(id: str, columns: list[str], rows: float, **fields: Any) -> Candidate:
    return make(
        need="dataset",
        id=id,
        description="customer churn",
        enriched=True,
        structure=DatasetStructure(columns=columns, total_rows=rows, splits={"default/train": rows}),
        metrics={"downloads": 50.0, "likes": 1.0},
        **fields,
    )


def folded(candidates: list[Candidate]) -> dict[str, list[str]]:
    leaders = fold_copies(list(score(candidates, phrases=["customer churn"]).values()))
    return {s.candidate.id: s.variants for s in leaders}


def test_uploads_with_the_same_columns_are_the_same_data_whatever_they_are_called() -> None:
    one = hub_dataset("a/telco-churn-7k", TELCO_COLUMNS, 7000, created_at=months_ago(30))
    two = hub_dataset(
        "b/phone-company-leavers", [c.upper() for c in TELCO_COLUMNS], 7000, created_at=months_ago(3)
    )
    other = hub_dataset(
        "c/bank-exits", ["RowNumber", "Surname", "CreditScore", "Geography", "Exited"], 10_000
    )
    assert folded([one, two, other]) == {"a/telco-churn-7k": ["b/phone-company-leavers"], "c/bank-exits": []}


def test_a_translation_keeps_the_shape_of_its_source_and_is_not_a_copy() -> None:
    english = hub_dataset("lab/imdb", ["text", "label"], 100_000)
    javanese = hub_dataset("x/imdb-javanese", ["text", "label"], 100_000)
    assert folded([english, javanese]) == {"lab/imdb": [], "x/imdb-javanese": []}


def test_kaggle_uploads_of_the_same_file_are_the_same_data() -> None:
    def upload(id: str, title: str, size: int, columns: int, notebooks: float, year: int) -> Candidate:
        c = kaggle_dataset(id, title, year, "MIT", notebooks, size=size)
        c.structure = DatasetStructure(file_types=[".csv"], column_count=columns)
        return c

    original = upload("first/bank-customer-churn-modeling", "Bank Turnover Dataset", 267_794, 14, 400, 2019)
    renamed = upload("copy/customer-churn-dataset", "Customer Churn Dataset", 267_794, 14, 20, 2024)
    near = upload("copy/telcom-leavers", "Telcom Leavers", 268_100, 14, 5, 2025)  # 0.1% apart, same width
    unrelated = upload("other/customer-churn", "Customer Churn", 46_575, 11, 250, 2020)
    same_size_only = upload(
        "other/shop-returns", "Shop Returns", 267_800, 9, 30, 2022
    )  # nothing else in common
    result = folded([original, renamed, near, unrelated, same_size_only])
    assert set(result) == {"first/bank-customer-churn-modeling", "other/customer-churn", "other/shop-returns"}
    assert set(result["first/bank-customer-churn-modeling"]) == {
        "copy/customer-churn-dataset",
        "copy/telcom-leavers",
    }


def test_a_wide_table_of_the_same_width_links_a_related_name_across_platforms() -> None:
    kaggle = kaggle_dataset("first/telco-customer-churn", "Telco Customer Churn", 2018, "Other", 2995)
    kaggle.structure = DatasetStructure(file_types=[".csv"], column_count=21)
    wide = [f"column_{n}" for n in range(21)]
    renamed = hub_dataset("org/telco-churn-7k", wide, 7000, created_at=months_ago(14))
    other_columns = [f"field_{n}" for n in range(21)]
    unrelated_name = hub_dataset("org/hr-attrition", other_columns, 7000, created_at=months_ago(14))
    narrow = hub_dataset("org/telco-churn-mini", ["a", "b", "c", "d", "e"], 7000)
    result = folded([kaggle, renamed, unrelated_name, narrow])
    # Same width and a related name: a copy. Same width alone, or a related name alone: not shown to be.
    assert result["first/telco-customer-churn"] == ["huggingface:org/telco-churn-7k"]
    assert set(result) == {"first/telco-customer-churn", "org/hr-attrition", "org/telco-churn-mini"}
