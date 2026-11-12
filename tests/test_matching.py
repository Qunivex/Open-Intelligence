"""How capability phrases are matched to what a repository says about itself.

Coverage decides the verdict, so both kinds of error matter: a word form that fails to match reports a
gap that is not there, and words gathered from unrelated lines report coverage that is not there.
"""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from conftest import NOW
from reuse_layer.config import RankingSettings
from reuse_layer.core import ranking
from reuse_layer.core.cards import build_card
from reuse_layer.core.models import Card, Repo, Scored
from reuse_layer.core.relevance import LexicalJudge, Need
from reuse_layer.core.text import stem, tokens


def repo(name: str, description: str = "", topics: tuple[str, ...] = ()) -> Repo:
    return Repo(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description=description,
        topics=topics,
        language="Python",
        license_spdx="MIT",
        archived=False,
        disabled=False,
        fork=False,
        is_template=False,
        created_at=NOW - timedelta(days=900),
        pushed_at=NOW - timedelta(days=3),
        default_branch="main",
        owner_login=name.split("/")[0],
        owner_type="Organization",
        stars=10,
        forks=10,
        open_issues=1,
    )


def card(name: str, description: str, readme: str = "", topics: tuple[str, ...] = ()) -> Card:
    return build_card(repo(name, description, topics), readme)


def judge(capability: str, c: Card, task: str = "a library"):  # type: ignore[no-untyped-def]
    return LexicalJudge().judge(Need(task=task, capabilities=(capability,)), c)


# --------------------------------------------------------------------------- word forms


@pytest.mark.parametrize(
    "family",
    [
        ("cache", "caches", "caching", "cached"),
        ("log", "logs", "logging", "logged"),
        ("parse", "parses", "parsing", "parsed"),
        ("route", "routes", "routing", "routed"),
        ("schedule", "schedules", "scheduling", "scheduled"),
        ("embed", "embeds", "embedding", "embeddings", "embedded"),
        ("map", "maps", "mapping", "mapped"),
        ("type", "typing", "typed"),
        ("price", "prices", "pricing", "priced"),
        ("api", "apis"),
        ("key", "keys"),
        ("query", "queries", "querying", "queried"),
        ("cookie", "cookies"),
        ("verify", "verifies", "verifying", "verified"),
        ("retry", "retries", "retrying", "retried"),
        ("deploy", "deploys", "deploying", "deployed"),
        ("process", "processes", "processing", "processed"),
        ("estimate", "estimates", "estimating", "estimated", "estimation", "estimator"),
        ("token", "tokens", "tokenize", "tokenized", "tokenizer", "tokenization"),
        ("add", "adds", "adding", "added"),
    ],
)
def test_forms_of_one_word_share_a_stem(family: tuple[str, ...]) -> None:
    assert len({stem(w) for w in family}) == 1, {w: stem(w) for w in family}


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("pipe", "pip"),  # every Python README says "pip"
        ("note", "not"),
        ("rate", "rat"),
        ("generate", "general"),  # derivations are deliberately not folded
        ("generate", "generic"),
        ("server", "serve"),
        ("policy", "police"),
        ("state", "stat"),
        ("string", "str"),
        ("model", "mode"),
    ],
)
def test_different_words_keep_different_stems(a: str, b: str) -> None:
    assert stem(a) != stem(b), (stem(a), stem(b))


def test_short_words_and_stopwords_are_left_alone() -> None:
    assert [stem(w) for w in ("is", "bus", "class", "redis", "status", "sing", "speed")] == [
        "is",
        "bus",
        "class",
        "redis",
        "status",
        "sing",
        "speed",
    ]
    assert tokens("Caching of API responses") == ["cach", "api", "respons"]


@pytest.mark.parametrize(
    ("capability", "bullet"),
    [
        ("response caching | cache responses", "Caches responses in Redis"),
        ("structured logging | JSON logs", "Structured logs as JSON"),
        ("request routing | route requests", "Routes each request to a handler"),
        ("job scheduling | scheduled jobs", "Schedule recurring jobs with cron syntax"),
        ("text embeddings | embed documents", "Embeds documents for semantic search"),
        ("retry failed requests | automatic retries", "Retries failed requests with backoff"),
    ],
)
def test_a_capability_is_found_whatever_the_word_form(capability: str, bullet: str) -> None:
    got = judge(capability, card("acme/toolkit", "A toolkit", f"## Features\n- {bullet}\n"))
    # "scheduling"/"schedule" and "embeddings"/"embeds" were reported as gaps before the stemmer fix
    assert got.covered and got.found_in == ("readme",), got


# --------------------------------------------------------------------------- one statement, not a pile of words


def test_words_scattered_over_unrelated_lines_are_not_coverage() -> None:
    currency = card(
        "acme/fx-tools",
        "Currency conversion helpers",
        "## Features\n- Live exchange rate lookup\n- No limits on history depth\n- Works per account\n",
    )
    got = judge("rate limiting | request throttling", currency, task="API gateway")
    assert got.missing == ("rate limiting",) and not got.covered  # "rate" and "limits" never meet


def test_the_same_words_in_one_statement_are_coverage() -> None:
    gateway = card("acme/gate", "API gateway", "## Features\n- Rate limiting per client\n- Request logging\n")
    got = judge("rate limiting | request throttling", gateway, task="API gateway")
    assert got.covered == ("rate limiting",) and got.found_in == ("readme",)


def test_a_readme_statement_is_read_in_the_context_of_what_the_project_is() -> None:
    gateway = card(
        "BerriAI/litellm",
        "Proxy server (LLM gateway) to call 100+ LLM APIs",
        "## Features\n- Cost tracking and spend management\n",
    )
    got = judge("LLM cost tracking", gateway)
    assert got.covered and got.found_in == (
        "readme",
    )  # "LLM" from its identity, "cost tracking" from the bullet
    # but the statement has to say something itself: identity alone does not make a README line relevant
    unrelated = card("acme/llm-notes", "Notes about LLM papers", "## Features\n- Weekly reading list\n")
    assert judge("LLM cost tracking", unrelated).missing == ("LLM cost tracking",)


def test_where_the_evidence_was_found_is_reported() -> None:
    named = card("acme/tokencost", "Price estimates for LLMs", topics=("llm", "cost-estimation"))
    got = judge("LLM cost estimation", named)
    assert got.covered and got.found_in == ("metadata",)  # name, topics and description, no README needed
    both = LexicalJudge().judge(
        Need(task="t", capabilities=("LLM cost estimation", "token counting", "audio transcription")),
        card(
            "acme/tokencost",
            "Price estimates for LLMs",
            "## Features\n- Token counting for prompts\n",
            ("llm", "cost-estimation"),
        ),
    )
    assert both.covered == ("LLM cost estimation", "token counting")
    assert both.found_in == ("metadata", "readme") and both.missing == ("audio transcription",)


def test_an_avoided_sense_still_demotes_a_card() -> None:
    finance = card("q/optionpricer", "Option pricing models: Black-Scholes and binomial trees")
    got = LexicalJudge().judge(
        Need(task="LLM cost", capabilities=("model prices",), avoid=("option pricing",)), finance
    )
    assert got.avoided == "option pricing"


# --------------------------------------------------------------------------- the draw must not move the verdict


def _scored(
    name: str, total_parts: tuple[float, float], *, covered: int, missing: int, newer: bool
) -> Scored:
    rel, trust = total_parts
    s = Scored(repo=repo(name), relevance=rel, trust=trust, verified=1.0, tier_score=1.0, newer=newer)
    s.covered = tuple(f"have {i}" for i in range(covered))
    s.missing = tuple(f"gap {i}" for i in range(missing))
    s.total = ranking.total(s, RankingSettings())
    return s


def test_the_explore_draw_never_changes_how_much_is_covered() -> None:
    def picks_for(seed: int) -> list[Scored]:
        established = _scored("e/partial", (0.8, 0.9), covered=2, missing=1, newer=False)
        complete = _scored("n/complete", (0.7, 0.6), covered=3, missing=0, newer=True)
        shiny = _scored(
            "n/shiny", (0.95, 0.95), covered=2, missing=1, newer=True
        )  # scores higher, covers less
        return ranking.select([established, complete, shiny], RankingSettings(), random.Random(seed))

    verdicts = set()
    for seed in range(40):
        picks = picks_for(seed)
        explore = next(p for p in picks if p.explore)
        assert explore.repo.full_name == "n/complete"  # was: either, depending on the draw
        verdicts.add(ranking.verdict(3, picks, RankingSettings(), must_have=3).verdict)
    assert verdicts == {"reuse"}  # was: reuse on some calls, reuse_partial on others
