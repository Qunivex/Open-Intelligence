from __future__ import annotations

import pytest

from open_intelligence.ranking.licenses import allows_commercial_use, classify
from open_intelligence.ranking.text import Vocabulary, coverage, jaccard, same_word, words


def test_words_drop_noise_and_keep_technical_names() -> None:
    assert words("Find the best open-source library for Node.js and C++") == ["library", "node.js", "c++"]
    assert words("text-classification models") == ["text", "classification", "model"]
    assert words("transformers.js") == ["transformers.js"]


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("classify", "classification", True),
        ("classify", "classifier", True),
        ("train", "training", True),
        ("generate", "generation", True),
        ("recognize", "recognition", True),
        ("sentiment", "sentimental", True),
        ("model", "modern", False),
        ("port", "portfolio", False),
        ("data", "database", False),
        ("interface", "interference", False),
        ("etl", "etl", True),
    ],
)
def test_same_word(a: str, b: str, same: bool) -> None:
    assert same_word(a, b) is same
    assert same_word(b, a) is same


def test_coverage_gives_less_credit_for_matches_in_the_long_text_only() -> None:
    strong = Vocabulary(["acme/census-loader", "Load Census data", "snowflake etl"])
    weak = Vocabulary(["Supports scheduling with cron and retries."])
    assert coverage("census snowflake", strong, weak) == 1.0
    assert coverage("scheduling", strong, weak) == pytest.approx(0.7)
    assert coverage("census scheduling kafka", strong, weak) == pytest.approx((1.0 + 0.7 + 0.0) / 3)
    assert coverage("the of", strong, weak) == 0.0


def test_camel_case_names_match_spaced_phrases() -> None:
    vocabulary = Vocabulary(["kalhosni/CustomerChurnTelecom", "PyTorch"])
    assert "customer" in vocabulary
    assert "churn" in vocabulary
    assert "pytorch" in vocabulary


def test_jaccard() -> None:
    assert jaccard("census-snowflake-sync", "census_snowflake_sync") == 1.0
    assert jaccard("telco-customer-churn", "bank-customer-churn") == pytest.approx(0.5)
    assert jaccard("", "x") == 0.0


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("apache-2.0", "permissive"),
        ("MIT", "permissive"),
        ("cc-by-4.0", "permissive"),
        ("0bsd", "permissive"),
        ("Attribution 4.0 International (CC BY 4.0)", "permissive"),
        ("CC0: Public Domain", "public-domain"),
        ("U.S. Government Works", "public-domain"),
        ("lgpl-3.0", "weak-copyleft"),
        ("cc-by-sa-4.0", "weak-copyleft"),
        ("Database: Open Database, Contents: Database Contents", "weak-copyleft"),
        ("Community Data License Agreement - Sharing - Version 1.0", "weak-copyleft"),
        ("GPL 2", "copyleft"),
        ("agpl-3.0", "copyleft"),
        ("cc-by-nc-sa-4.0", "non-commercial"),
        ("Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)", "non-commercial"),
        ("openrail", "use-restricted"),
        ("llama3.1", "use-restricted"),
        ("cc-by-nd-4.0", "use-restricted"),
        ("World Bank Dataset Terms of Use", "use-restricted"),
        # Not a grant, whatever else the string says.
        ("Data files © Original Authors", "unclear"),
        ("Database: Open Database, Contents: © Original Authors", "unclear"),
        ("Other (specified in description)", "unclear"),
        ("other", "unclear"),
        ("NOASSERTION", "unclear"),
        ("Unknown", "unclear"),
        (None, "unclear"),
    ],
)
def test_license_kinds(raw: str | None, kind: str) -> None:
    assert classify(raw) == kind


def test_commercial_use() -> None:
    assert allows_commercial_use("permissive") is True
    assert allows_commercial_use("non-commercial") is False
    assert allows_commercial_use("unclear") is None
