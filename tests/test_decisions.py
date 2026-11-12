"""Component decisions: evidence that is missing, must-haves that are not covered, and honest wording.

Each test names the rule it protects:

* incomplete or rate-limited evidence is ``inconclusive``, never ``no_candidates`` or ``build_yourself``;
* a must-have capability gap cannot be outranked or hidden by trust, popularity or a model judge;
* one call is one component's decision, with its own recommendation, coverage, gate results,
  unresolved checks and integration risks;
* gates stay fail-closed, and nothing the layer says claims a project is safe or fully compatible.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from mcp import Client

from conftest import NOW, FakeGitHub, FakeRepo, make_repo, make_settings, run
from reuse_layer.adapters.cli import _print_find
from reuse_layer.adapters.mcp_server import SERVER_INSTRUCTIONS, build_server, discovery_workflow
from reuse_layer.config import GitHubSettings, RankingSettings
from reuse_layer.core import ranking
from reuse_layer.core.engine import LIMITS, Engine
from reuse_layer.core.gates import GatePolicy, evidence_gates, unresolved_checks
from reuse_layer.core.models import (
    Advisory,
    CallerContext,
    DeepFacts,
    FindRequest,
    GateResult,
    Pin,
    Repo,
    Scored,
)
from reuse_layer.sources.base import RateLimited

PRICES = "up-to-date per-token prices for many LLM models | LLM pricing data | model prices"
COUNTING = "count tokens for prompts and completions | token counting | tokenizer"
COSTING = "calculate cost of LLM API calls | LLM cost estimation | cost tracking"
COUNTING_LABEL = "count tokens for prompts and completions"
TASK = (
    "Estimate and compare the cost of LLM API usage across providers from token counts and per-token prices"
)

# A well-run, popular gateway that does two of the three things and has no token counting.
GATEWAY = FakeRepo(
    "BerriAI/litellm",
    "Python SDK, Proxy Server (LLM Gateway) to call 100+ LLM APIs in OpenAI format",
    topics=["llm", "openai", "gateway", "anthropic"],
    org_verified=True,
    contributors=100,
    capped=True,
    forks=4000,
    stars=30000,
    created_days=1100,
    readme="# LiteLLM\n\nCall all LLM APIs using the OpenAI format.\n\n## Features\n"
    "- Cost tracking and spend management across providers\n"
    "- Model prices and context window JSON for every model\n- Load balancing and fallbacks\n",
)
# A small personal project that does all three.
SMALL_BUT_COMPLETE = FakeRepo(
    "solo/llm-cost-kit",
    "LLM token cost estimation",
    topics=["llm", "cost-estimation"],
    owner_type="User",
    contributors=2,
    releases=1,
    health=40,
    ci="none",
    stars=3,
    forks=0,
    created_days=400,
    readme="# llm-cost-kit\n\n## Features\n- Up-to-date per-token prices for many LLM models\n"
    "- Count tokens for prompts and completions\n- Calculate the cost of LLM API calls\n",
)
TOKENIZER = FakeRepo(
    "openai/tiktoken",
    "tiktoken is a fast BPE tokeniser for use with OpenAI's models.",
    org_verified=True,
    contributors=30,
    forks=900,
    created_days=1300,
    releases=3,
    readme="# tiktoken\n\nA fast BPE tokeniser.\n\n## Features\n- Count tokens for a prompt\n",
)


def world_of(*repos: FakeRepo) -> FakeGitHub:
    gh = FakeGitHub()
    gh.add(*repos)
    return gh


def request(*candidates: str, **kw: Any) -> FindRequest:
    base: dict[str, Any] = dict(
        task=TASK,
        capabilities=[PRICES, COUNTING, COSTING],
        candidates=list(candidates),
        context=CallerContext(language="python"),
    )
    base.update(kw)
    return FindRequest(**base)


def names(out: dict[str, Any]) -> list[str]:
    return [p["name"] for p in out["picks"]]


# --------------------------------------------------------------------------- 1. incomplete evidence


def test_rate_limited_search_is_inconclusive_not_no_candidates(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    world.rate_limit_paths.add("/search/repositories")
    out = run(make_engine(world).find_reusable(request(search_queries=["llm cost estimation"])))
    assert out["verdict"] == "inconclusive"
    assert out["evidence"]["status"] == "unavailable"
    assert out["build_yourself"] == {"recommended": False}  # nobody is told to build on missing evidence
    assert out["considered"] == 0 and out["picks"] == []
    assert any(
        c.startswith("search not completed (rate limited): llm cost") for c in out["unresolved_checks"]
    )
    assert "retry" in out["recommendation"]["next"]
    assert any("rate limit" in n for n in out["notes"])
    # the must-haves are unknown, not missing: nothing was looked at
    assert out["coverage"]["missing"] == [] and len(out["coverage"]["unknown"]) == 3


def test_a_search_that_finds_nothing_is_a_genuine_no_candidates(make_engine: Callable[..., Engine]) -> None:
    out = run(make_engine(world_of(GATEWAY)).find_reusable(request(search_queries=["quantum annealing"])))
    assert out["verdict"] == "no_candidates"
    assert out["evidence"]["status"] == "complete"
    assert out["build_yourself"]["recommended"] is True
    assert "no public repository matched" in out["build_yourself"]["reason"]
    assert not any("search" in c for c in out.get("unresolved_checks", []))


def test_a_failed_search_is_not_an_empty_search(make_engine: Callable[..., Engine]) -> None:
    gh = world_of(GATEWAY)
    gh.fail_paths.add("/search/repositories")  # HTTP 500: used to vanish with only an info log
    out = run(make_engine(gh).find_reusable(request(search_queries=["llm cost estimation"])))
    assert out["verdict"] == "inconclusive"
    assert any(c.startswith("search not completed (GitHub error)") for c in out["unresolved_checks"])


def test_a_candidate_that_could_not_be_verified_is_not_reported_as_invented(
    make_engine: Callable[..., Engine],
) -> None:
    gh = world_of(GATEWAY)
    gh.fail_paths.add("/repos/BerriAI/litellm")
    out = run(make_engine(gh).find_reusable(request("BerriAI/litellm", "made-up/nothing")))
    assert out["verdict"] == "inconclusive"
    assert out["unresolved"] == ["made-up/nothing"]  # invented: GitHub said it does not exist
    assert "candidate not verified (GitHub error): BerriAI/litellm" in out["unresolved_checks"]


def _hang(engine: Engine, *slow: str) -> None:
    """Make the evidence pass for the named repositories (or all of them) outlast the deadline."""
    source = engine.sources["repository"]
    real = source.enrich

    async def enrich(repo: Repo) -> DeepFacts:
        if not slow or repo.full_name in slow:
            await asyncio.sleep(60)
        return await real(repo)

    source.enrich = enrich  # type: ignore[method-assign]


SHORT_DEADLINE = make_settings(github=GitHubSettings(deadline_ms=1000))


def test_evidence_timeout_is_unchecked_not_failed(make_engine: Callable[..., Engine]) -> None:
    engine = make_engine(world_of(GATEWAY, SMALL_BUT_COMPLETE), SHORT_DEADLINE)
    _hang(engine)
    out = run(engine.find_reusable(request("BerriAI/litellm", "solo/llm-cost-kit")))
    assert out["picks"] == []  # fails closed
    assert out["verdict"] == "inconclusive"  # was: no_candidates, "every candidate failed a ... gate"
    assert out["evidence"] == {"status": "partial", "deep_checked": 0}
    for r in out["rejected"]:
        assert r["gate"] == "evidence" and r["unchecked"] is True and "time budget" in r["reason"]
    what, _, about = out["unresolved_checks"][0].partition(": ")
    assert what == "evidence checks did not run (time budget); fails closed"
    assert sorted(about.split("; ")) == ["BerriAI/litellm", "solo/llm-cost-kit"]  # one line per kind of gap


def test_one_timeout_keeps_a_positive_verdict_but_marks_the_evidence_partial(
    make_engine: Callable[..., Engine],
) -> None:
    engine = make_engine(world_of(GATEWAY, SMALL_BUT_COMPLETE), SHORT_DEADLINE)
    _hang(engine, "BerriAI/litellm")
    out = run(engine.find_reusable(request("BerriAI/litellm", "solo/llm-cost-kit")))
    assert out["verdict"] == "reuse" and names(out) == ["solo/llm-cost-kit"]
    assert out["evidence"]["status"] == "partial"
    assert "Evidence is partial" in out["recommendation"]["reason"]
    assert any(c.endswith("fails closed: BerriAI/litellm") for c in out["unresolved_checks"])
    assert {r["repo"]: r.get("unchecked") for r in out["rejected"]} == {"BerriAI/litellm": True}


def test_rate_limited_evidence_pass_is_inconclusive(make_engine: Callable[..., Engine]) -> None:
    engine = make_engine(world_of(GATEWAY))

    async def limited(repo: Repo) -> DeepFacts:
        raise RateLimited("GitHub rate limit reached")

    engine.sources["repository"].enrich = limited  # type: ignore[method-assign]
    out = run(engine.find_reusable(request("BerriAI/litellm")))
    assert out["verdict"] == "inconclusive" and out["picks"] == []
    assert out["unresolved_checks"][0] == (
        "evidence checks did not run (rate limited); fails closed: BerriAI/litellm"
    )


def test_a_known_blocking_advisory_is_a_real_failure_even_when_another_source_did_not_answer(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    world.fail_paths.add("/security-advisories")  # the repository source fails; the advisory database answers
    out = run(make_engine(world).find_reusable(request("vulnco/llm-cost-meter", "AgentOps-AI/tokencost")))
    rejected = {r["repo"]: r for r in out["rejected"]}
    assert "GHSA-aaaa-bbbb-cccc" in rejected["vulnco/llm-cost-meter"]["reason"]
    assert "unchecked" not in rejected["vulnco/llm-cost-meter"]  # found, not unknown
    assert rejected["AgentOps-AI/tokencost"]["unchecked"] is True
    assert out["verdict"] == "inconclusive"


def test_weak_fit_on_incomplete_evidence_is_not_a_build_verdict(make_engine: Callable[..., Engine]) -> None:
    hud = FakeRepo("acme/hud-kit", "Generic dashboard widgets", topics=["dashboard"])
    req = FindRequest(
        task="Spaceship cockpit HUD with flight controls for our WebXR game",
        capabilities=["cockpit HUD overlay | heads-up display", "flight controls | joystick input"],
        candidates=["acme/hud-kit"],
        search_queries=["webxr cockpit hud"],
    )
    complete = run(make_engine(world_of(hud)).find_reusable(req))
    assert complete["verdict"] == "build_yourself" and complete["build_yourself"]["recommended"] is True

    gh = world_of(hud)
    gh.fail_paths.add("/search/repositories")
    partial = run(make_engine(gh).find_reusable(req))
    assert partial["verdict"] == "inconclusive" and partial["build_yourself"]["recommended"] is False
    assert names(partial) == ["acme/hud-kit"]  # still shown, just not grounds for a conclusion


def test_candidates_left_unexamined_rule_out_no_candidates(make_engine: Callable[..., Engine]) -> None:
    def vulnerable(name: str) -> FakeRepo:
        repo = name.split("/")[1]
        return FakeRepo(
            name,
            "LLM cost estimation and token pricing",
            topics=["llm", "cost-estimation"],
            advisories=[
                {
                    "ghsa_id": f"GHSA-{repo[:4]}-0000-0000",
                    "type": "reviewed",
                    "severity": "critical",
                    "summary": "Remote code execution",
                    "source_code_location": f"https://github.com/{name}",
                    "vulnerabilities": [{"package": {"name": repo}, "first_patched_version": None}],
                }
            ],
        )

    bad = ["aaa/llm-cost-one", "bbb/llm-cost-two", "ccc/llm-cost-three"]
    quiet = FakeRepo(
        "ddd/llm-kit", "Utilities", readme="# llm-kit\n\n## Features\n- Estimate the cost of LLM API calls\n"
    )
    gh = world_of(*(vulnerable(b) for b in bad), quiet)
    settings = make_settings(github=GitHubSettings(deep_enrich=3))
    req = FindRequest(
        task="LLM cost estimation", search_queries=["llm"], context=CallerContext(language="python")
    )

    out = run(make_engine(gh, settings).find_reusable(req))
    assert out["picks"] == [] and {r["repo"] for r in out["rejected"]} == set(bad)
    assert out["verdict"] == "inconclusive"  # was: no_candidates, though one match was never examined
    assert out["evidence"] == {"status": "complete", "deep_checked": 3, "not_examined": 1}
    assert "context.exclude" in out["recommendation"]["next"]

    again = run(
        make_engine(gh, settings).find_reusable(
            req.model_copy(update={"context": CallerContext(language="python", exclude=bad)})
        )
    )
    assert again["verdict"] == "reuse" and names(again) == ["ddd/llm-kit"]


# --------------------------------------------------------------------------- 2. capability gaps


def test_high_trust_project_with_a_gap_gets_reuse_partial(make_engine: Callable[..., Engine]) -> None:
    out = run(make_engine(world_of(GATEWAY)).find_reusable(request("BerriAI/litellm")))
    pick = out["picks"][0]
    assert pick["scores"]["trust"] > 0.9 and pick["scores"]["total"] > 0.6  # excellent on everything else
    assert out["verdict"] == "reuse_partial"  # was: reuse, "1 ... project cover this task"
    assert out["recommendation"]["repo"] == "BerriAI/litellm"
    assert "2 of 3 must-have capabilities" in out["recommendation"]["reason"]
    assert out["coverage"]["must_have"] == 3 and len(out["coverage"]["covered"]) == 2
    assert out["coverage"]["missing"] == [COUNTING_LABEL]
    assert pick["integration_risks"][0] == f"No evidence for must-have: {COUNTING_LABEL}"
    assert out["build_yourself"]["recommended"] is False
    assert "missing" in out["recommendation"]["next"]


def test_full_coverage_ranks_above_a_higher_scoring_project_with_a_gap(
    make_engine: Callable[..., Engine],
) -> None:
    gh = world_of(GATEWAY, SMALL_BUT_COMPLETE)
    out = run(make_engine(gh).find_reusable(request("BerriAI/litellm", "solo/llm-cost-kit")))
    assert names(out) == ["solo/llm-cost-kit", "BerriAI/litellm"]
    small, gateway = out["picks"]
    assert gateway["scores"]["total"] > small["scores"]["total"]  # trust and tier favour the gateway...
    assert gateway["scores"]["trust"] > small["scores"]["trust"]
    assert out["verdict"] == "reuse"  # ...but only the small project does everything that was asked
    assert out["recommendation"]["repo"] == "solo/llm-cost-kit"
    assert out["coverage"]["missing"] == []
    assert gateway["integration_risks"][0] == f"No evidence for must-have: {COUNTING_LABEL}"


def test_a_gap_covered_by_another_pick_is_pointed_out(make_engine: Callable[..., Engine]) -> None:
    gh = world_of(GATEWAY, TOKENIZER)
    out = run(make_engine(gh).find_reusable(request("BerriAI/litellm", "openai/tiktoken")))
    assert out["verdict"] == "reuse_partial" and out["recommendation"]["repo"] == "BerriAI/litellm"
    assert out["coverage"]["covered_by_other_picks"] == {COUNTING_LABEL: ["openai/tiktoken"]}


def test_a_model_judge_cannot_turn_a_gap_into_coverage(make_engine: Callable[..., Engine]) -> None:
    class Enthusiast:
        model = "fake-judge"

        async def score(self, need: Any, cards: Any) -> dict[str, float]:
            return {c.repo: 1.0 for c in cards}

    lexical = run(make_engine(world_of(GATEWAY)).find_reusable(request("BerriAI/litellm")))
    engine = make_engine(world_of(GATEWAY))
    engine.llm = Enthusiast()  # type: ignore[assignment]
    out = run(engine.find_reusable(request("BerriAI/litellm")))
    assert out["relevance_judge"] == "fake-judge + lexical"
    # the judge raised the score...
    assert out["picks"][0]["scores"]["relevance"] > lexical["picks"][0]["scores"]["relevance"] + 0.15
    assert out["verdict"] == "reuse_partial"  # ...the gap is still a gap
    assert out["coverage"]["missing"] == [COUNTING_LABEL]


def _repo(name: str, stars: int = 10) -> Repo:
    return make_repo(name, stars=stars)


def _scored(
    name: str,
    rel: float,
    trust: float,
    *,
    covered: int = 0,
    missing: int = 0,
    stars: int = 10,
    newer: bool = False,
) -> Scored:
    s = Scored(repo=_repo(name, stars), relevance=rel, trust=trust, verified=1.0, tier_score=1.0, newer=newer)
    s.covered = tuple(f"have {i}" for i in range(covered))
    s.missing = tuple(f"gap {i}" for i in range(missing))
    s.total = ranking.total(s, RankingSettings())
    return s


def test_coverage_orders_picks_before_score_and_stars() -> None:
    complete = _scored("a/complete", 0.6, 0.1, covered=3)
    trusted = _scored("b/trusted", 0.9, 1.0, covered=2, missing=1, stars=500_000)
    worse = _scored("c/worse", 0.95, 1.0, covered=1, missing=2, stars=900_000)
    assert trusted.total > complete.total and worse.total > complete.total
    picks = ranking.select([worse, trusted, complete], RankingSettings(), random.Random(1))
    assert [p.repo.full_name for p in picks] == ["a/complete", "b/trusted", "c/worse"]


def test_explore_slot_never_displaces_better_coverage() -> None:
    established = [_scored(f"e/{i}", 0.8, 0.8, covered=3) for i in range(3)]
    shiny = _scored("n/shiny", 0.95, 0.95, covered=2, missing=1, newer=True)
    picks = ranking.select([*established, shiny], RankingSettings(), random.Random(3))
    assert "n/shiny" not in [p.repo.full_name for p in picks]
    equal = _scored("n/equal", 0.95, 0.95, covered=3, newer=True)
    picks = ranking.select([*established, equal], RankingSettings(), random.Random(3))
    assert sum(p.explore for p in picks) == 1


def test_verdict_semantics() -> None:
    w = RankingSettings()
    full = _scored("a/full", 0.8, 0.9, covered=3)
    gap = _scored("a/gap", 0.8, 0.9, covered=2, missing=1)
    none = _scored("a/none", 0.8, 0.9, missing=3)
    weak = _scored("a/weak", 0.2, 0.9, covered=3)

    assert ranking.verdict(5, [full], w, must_have=3)[:2] == ("reuse", False)
    assert ranking.verdict(5, [gap], w, must_have=3)[:2] == ("reuse_partial", False)
    assert "gap 0" in ranking.verdict(5, [gap], w, must_have=3).reason
    assert ranking.verdict(5, [none], w, must_have=3)[:2] == (
        "build_yourself",
        True,
    )  # relevant, covers nothing
    assert ranking.verdict(5, [weak], w, must_have=3)[:2] == ("build_yourself", True)
    assert ranking.verdict(5, [weak, gap], w, must_have=3).recommended is gap  # first that qualifies
    assert ranking.verdict(0, [], w)[:2] == ("no_candidates", True)
    assert ranking.verdict(5, [], w)[:2] == ("no_candidates", True)

    # incomplete decisive evidence never produces a negative verdict, and never recommends building
    assert ranking.verdict(0, [], w, gaps=2)[:2] == ("inconclusive", False)
    assert ranking.verdict(5, [], w, gaps=1)[:2] == ("inconclusive", False)
    assert ranking.verdict(5, [weak], w, must_have=3, gaps=1)[:2] == ("inconclusive", False)
    assert ranking.verdict(5, [], w, unexamined=4)[:2] == ("inconclusive", False)
    # a pick that passed its own gates still stands, with the caveat in the reason
    partial = ranking.verdict(5, [full], w, must_have=3, gaps=1)
    assert partial.verdict == "reuse" and "Evidence is partial" in partial.reason
    # unexamined candidates do not block a build verdict: the best-looking ones were read in full
    assert ranking.verdict(5, [weak], w, must_have=3, unexamined=4).verdict == "build_yourself"
    # without must-haves, coverage is said to be unassessed rather than assumed
    assert "coverage was not assessed" in ranking.verdict(5, [_scored("a/x", 0.8, 0.9)], w).reason


# --------------------------------------------------------------------------- 3. one component, one decision


def test_each_component_gets_its_own_decision(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    engine = make_engine(world)
    counting = run(
        engine.find_reusable(
            request(
                "openai/tiktoken",
                "BerriAI/litellm",
                component="token counting",
                capabilities=[COUNTING],
            )
        )
    )
    hud = run(
        engine.find_reusable(
            FindRequest(
                task="Spaceship cockpit HUD with flight controls for a WebXR game",
                component="cockpit HUD",
                capabilities=["cockpit HUD overlay | heads-up display"],
                candidates=["openai/tiktoken"],
                context=CallerContext(language="python"),
            )
        )
    )
    for out, name in ((counting, "token counting"), (hud, "cockpit HUD")):
        assert out["component"] == name
        # the five things every component decision carries
        assert {"recommendation", "coverage", "picks", "rejected", "evidence"} <= set(out)
        assert {"reason", "next"} <= set(out["recommendation"])
        assert out["coverage"]["must_have"] == 1
        for p in out["picks"]:
            assert {"status", "license", "vulns", "malware", "provenance"} <= set(p["gates"])
    assert counting["verdict"] == "reuse" and counting["recommendation"]["repo"] == "openai/tiktoken"
    # every pick carries the same identity fields, whichever source it came from
    top = counting["picks"][0]
    assert {
        "id": f"pkg:github/openai/tiktoken@{top['version']}",
        "provider": "github",
        "artifact_type": "repository",
        "name": "openai/tiktoken",
        "url": "https://github.com/openai/tiktoken",
        "license": "MIT",
    }.items() <= top.items()
    assert len(top["revision"]) == 40 and "pin" not in top and "normalized" not in top
    assert counting["coverage"] == {
        "must_have": 1,
        "covered": [COUNTING_LABEL],
        "missing": [],
        "found_in": ["metadata"],  # the repository's own name says "token"
    }
    gateway = next(p for p in counting["picks"] if p["name"].endswith("litellm"))
    assert gateway["integration_risks"][0].startswith("No evidence for must-have")
    assert hud["verdict"] == "build_yourself" and "repo" not in hud["recommendation"]
    assert hud["coverage"]["missing"] == ["cockpit HUD overlay"]
    assert counting["recommendation_id"] != hud["recommendation_id"]


def test_unresolved_checks_and_integration_risks_are_reported_per_pick(
    make_engine: Callable[..., Engine],
) -> None:
    copyleft = FakeRepo(
        "solo/llm-cost-kit",
        "LLM token cost estimation",
        topics=["llm"],
        license="GPL-3.0",
        owner_type="User",
        contributors=1,
        created_days=100,
        language=None,
        readme=SMALL_BUT_COMPLETE.readme,
    )
    gh = world_of(copyleft)
    gh.fail_paths.add("/check-runs")
    req = request("solo/llm-cost-kit", context=CallerContext(license_policy="any_osi"))
    out = run(make_engine(gh).find_reusable(req))
    pick = out["picks"][0]
    assert out["verdict"] == "reuse"
    assert pick["unresolved_checks"] == ["upstream CI status not read"]
    assert out["evidence"]["status"] == "partial"  # a check failed, though not a decisive one
    risks = " | ".join(pick["integration_risks"])
    assert "Single maintainer" in risks
    assert "Newer project (100 days old)" in risks
    assert "No primary language detected" in risks
    assert "GPL-3.0 is strong copyleft" in risks
    # the fit gate passed only because there was nothing to compare: that is said, not implied
    assert "language fit not checked: no project language was given" in out["unresolved_checks"]
    assert out["constraints"] == {"license_policy": "any_osi", "min_age_days": 30}


def test_unread_readme_is_reported_and_blocks_a_build_verdict(make_engine: Callable[..., Engine]) -> None:
    gh = world_of(SMALL_BUT_COMPLETE)
    gh.fail_paths.add("/readme")
    out = run(make_engine(gh).find_reusable(request("solo/llm-cost-kit")))
    assert out["picks"][0]["unresolved_checks"][0].startswith("README not read")
    assert out["verdict"] != "build_yourself" and out["evidence"]["status"] == "partial"
    assert "README not read, so capability coverage is unknown: solo/llm-cost-kit" in out["unresolved_checks"]


def test_gate_passing_projects_that_were_not_picked_are_listed(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    out = run(
        make_engine(world).find_reusable(
            request(
                "AgentOps-AI/tokencost",
                "BerriAI/litellm",
                "openai/tiktoken",
                search_queries=["llm cost estimation", "option pricing model"],
            )
        )
    )
    shortlist = set(names(out)) | set(out["also_passed"]) | {r["repo"] for r in out["rejected"]}
    assert out["considered"] == len(shortlist)  # every candidate found is accounted for
    assert not set(names(out)) & set(out["also_passed"])


def test_capability_without_assessable_words_is_reported(make_engine: Callable[..., Engine]) -> None:
    req = request("BerriAI/litellm", capabilities=[COSTING, "good tool"])
    out = run(make_engine(world_of(GATEWAY)).find_reusable(req))
    assert out["coverage"]["must_have"] == 1
    assert "1 capability phrase(s) had only generic words and were not assessed" in out["unresolved_checks"]


def test_component_round_trip_over_mcp(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    engine = make_engine(world)
    args = {
        "task": TASK,
        "component": "token counting",
        "capabilities": [COUNTING],
        "candidates": ["openai/tiktoken"],
        "context": {"language": "python"},
        "artifact_type": "repository",
    }

    async def go() -> Any:
        async with Client(build_server(lambda: engine)) as client:
            tools = (await client.list_tools()).tools
            return tools, await client.call_tool("find_reusable", args)

    tools, res = run(go())
    data = json.loads(res.content[0].text)
    assert data["component"] == "token counting" and data["verdict"] == "reuse"
    assert data["picks"][0]["provider"] == "github"
    assert data["picks"][0]["revision"][:12] in data["picks"][0]["get"]
    description = next(t.description for t in tools if t.name == "find_reusable")
    for verdict in ("reuse_partial", "build_yourself", "no_candidates", "inconclusive"):
        assert verdict in description
    assert "not proof that a project is safe or compatible" in description


def test_cli_prints_the_component_decision(capsys: pytest.CaptureFixture[str]) -> None:
    _print_find(
        {
            "component": "pricing",
            "verdict": "reuse_partial",
            "recommendation": {
                "repo": "a/b",
                "reason": "a/b shows evidence for 1 of 2",
                "next": "fill the gap",
            },
            "build_yourself": {"recommended": False},
            "coverage": {
                "must_have": 2,
                "covered": ["x"],
                "missing": ["y"],
                "covered_by_other_picks": {"y": ["c/d"]},
            },
            "evidence": {"status": "partial", "deep_checked": 2},
            "unresolved_checks": ["search not completed (rate limited): q"],
            "picks": [
                {
                    "id": "pkg:github/a/b@v1",
                    "scores": {"total": 0.7},
                    "summary": "s",
                    "why": [],
                    "version": "v1",
                    "integration_risks": ["No evidence for must-have: y"],
                    "unresolved_checks": ["upstream CI status not read"],
                    "get": "git clone",
                }
            ],
            "also_passed": ["e/f"],
            "rejected": [{"repo": "g/h", "gate": "evidence", "reason": "did not run", "unchecked": True}],
            "considered": 4,
            "relevance_judge": "lexical",
            "elapsed_ms": 1,
            "recommendation_id": "rec_x",
            "limits": LIMITS,
        }
    )
    text = capsys.readouterr().out
    for expected in (
        "component: pricing",
        "verdict: reuse_partial  (a/b shows evidence for 1 of 2)",
        "next: fill the gap",
        "coverage: 1/2 must-have capabilities evidenced",
        "- missing: y (evidenced by c/d)",
        "evidence: partial",
        "? search not completed (rate limited): q",
        "! No evidence for must-have: y",
        "? upstream CI status not read",
        "also passed the gates, ranked lower: e/f",
        "- g/h: evidence unchecked: did not run",
        LIMITS,
    ):
        assert expected in text


# --------------------------------------------------------------------------- 4. fail closed, no over-claiming

POLICY = GatePolicy("permissive", 30, 730, False, False, False, "python")
PINNED = Pin("v1.0.0", "release", "a" * 40, "valid")


def _gates(deep: DeepFacts | None, **kw: Any) -> dict[str, GateResult]:
    s = Scored(repo=_repo("acme/thing"), deep=deep, **kw)
    return {g.gate: g for g in evidence_gates(s, POLICY, None, NOW)}


def test_checks_that_could_not_complete_fail_closed_and_say_so() -> None:
    not_run = _gates(None, deep_error="rate limited")["evidence"]
    assert (not_run.passed, not_run.complete) == (False, False) and "rate limited" in not_run.detail
    no_vulns = _gates(DeepFacts(advisories_checked=False, pin=PINNED))["vulns"]
    assert (no_vulns.passed, no_vulns.complete) == (False, False)
    no_pin = _gates(DeepFacts(advisories_checked=True, pin=None))["provenance"]
    assert (no_pin.passed, no_pin.complete) == (False, False)

    # a real finding is a failure, not an unknown, whichever source it came from
    critical = Advisory("GHSA-1", "critical", "rce", patched=False)
    found = _gates(DeepFacts(advisories_checked=False, advisories=[critical], pin=PINNED))["vulns"]
    assert (found.passed, found.complete) == (False, True)
    bad_signature = _gates(DeepFacts(advisories_checked=True, pin=Pin("v1", "release", "b" * 40, "invalid")))
    assert (bad_signature["provenance"].passed, bad_signature["provenance"].complete) == (False, True)


def test_unchecked_is_only_for_candidates_nothing_was_found_against() -> None:
    license_failed = GateResult("license", False, "GPL-3.0 not allowed")
    vulns_unknown = GateResult("vulns", False, "could not complete", complete=False)
    ok = GateResult("status", True, "active")

    unknown = Scored(repo=_repo("a/a"), gates=[ok, vulns_unknown])
    assert not unknown.passed and unknown.unchecked
    assert ranking.total(unknown, RankingSettings()) == 0.0  # fail closed: it scores nothing

    both = Scored(repo=_repo("a/b"), gates=[ok, vulns_unknown, license_failed])
    assert not both.passed and not both.unchecked
    assert both.first_failure() is license_failed  # the real finding is the one reported

    passed = Scored(repo=_repo("a/c"), gates=[ok])
    assert passed.passed and not passed.unchecked and passed.first_failure() is None


def _vulns(*advisories: Advisory, checked: bool = True) -> tuple[GateResult, Scored]:
    deep = DeepFacts(advisories_checked=checked, pin=PINNED, advisories=list(advisories))
    s = Scored(repo=_repo("acme/thing"), deep=deep)
    return next(g for g in evidence_gates(s, POLICY, None, NOW) if g.gate == "vulns"), s


def test_high_or_critical_advisory_with_unknown_fix_status_fails_closed() -> None:
    for severity in ("high", "critical"):
        gate, _ = _vulns(Advisory("GHSA-9", severity, "rce in loader", patched=None))
        # a finding, not an unchecked candidate: the advisory was read and it cannot be cleared
        assert (gate.passed, gate.complete) == (False, True)
        assert "GHSA-9" in gate.detail and "fix status unknown (fails closed)" in gate.detail
    # one is enough, even next to advisories that do have a fix
    gate, _ = _vulns(
        Advisory("GHSA-1", "critical", "a", patched=True), Advisory("GHSA-2", "high", "b", patched=None)
    )
    assert not gate.passed and "GHSA-2" in gate.detail
    # an advisory known to be unfixed is still the one reported
    gate, _ = _vulns(
        Advisory("GHSA-2", "high", "b", patched=None), Advisory("GHSA-3", "critical", "c", patched=False)
    )
    assert not gate.passed and "GHSA-3" in gate.detail and "has no fixed version" in gate.detail
    # and it stays a finding when another advisory source did not answer
    gate, _ = _vulns(Advisory("GHSA-9", "high", "x", patched=None), checked=False)
    assert (gate.passed, gate.complete) == (False, True)


def test_lower_severity_unknown_fix_status_is_a_risk_and_never_reported_as_fixed() -> None:
    for severity in ("low", "medium"):
        gate, s = _vulns(Advisory("GHSA-7", severity, "x", patched=None))
        assert gate.passed and "all with fixes" not in gate.detail
        assert "fix status unknown for 1" in gate.detail
        assert s.risks == ["Fix status unknown for 1 low/medium advisories: review them"]
    fixed, s = _vulns(Advisory("GHSA-8", "critical", "x", patched=True))
    assert fixed.passed and "all with fixes" in fixed.detail and s.risks == []


def test_project_with_an_unclearable_critical_advisory_is_rejected_end_to_end(
    make_engine: Callable[..., Engine],
) -> None:
    advisory = {
        "ghsa_id": "GHSA-dddd-eeee-ffff",
        "type": "reviewed",
        "severity": "critical",
        "summary": "Deserialisation of untrusted price files",
        "source_code_location": "https://github.com/solo/llm-cost-kit",
        "vulnerabilities": [],  # the advisory names no affected or fixed versions
    }
    gh = world_of(replace(SMALL_BUT_COMPLETE, advisories=[advisory]))
    out = run(make_engine(gh).find_reusable(request("solo/llm-cost-kit")))
    assert out["picks"] == []  # it covers every must-have, and still is not offered
    (rejected,) = out["rejected"]
    assert rejected["gate"] == "vulns" and "unchecked" not in rejected
    assert "GHSA-dddd-eeee-ffff (critical)" in rejected["reason"]
    assert "fix status unknown (fails closed)" in rejected["reason"]
    # a finding on complete evidence, so this is a real "nothing usable", not an inconclusive result
    assert out["verdict"] == "no_candidates" and out["evidence"]["status"] == "complete"


def test_unresolved_checks_name_what_stayed_unknown() -> None:
    deep = DeepFacts(advisories_checked=True, pin=Pin("v1", "release", "c" * 40, "unknown"), ci="pending")
    deep.errors += ["readme: GitHub returned HTTP 500", "releases: timeout", "branch freshness: timeout"]
    got = unresolved_checks(Scored(repo=_repo("a/a"), deep=deep))
    assert got == [
        "branch freshness check did not complete",
        "README not read: capability coverage is judged from repository metadata only",
        "releases not listed: the pin may not be the latest release",
        "upstream CI still running on the pinned commit",
        "commit signature could not be read",
    ]
    assert unresolved_checks(Scored(repo=_repo("a/b"), deep=DeepFacts(pin=PINNED, ci="passing"))) == []


_CLAIM = re.compile(
    r"\b(is|are|it's|proven|verified|certified|guaranteed)\s+(completely |fully |totally )?(safe|secure|compatible)\b"
    r"|\bfully compatible\b|\bguarantee[sd]?\b|\bsafe to (use|install|run)\b|\bno (security )?risks?\b",
    re.IGNORECASE,
)


def _own_text(value: Any, key: str = "") -> list[str]:
    """Every string the layer wrote itself (repository-supplied summaries are not its claims)."""
    if isinstance(value, dict):
        return [t for k, v in value.items() if k != "summary" for t in _own_text(v, k)]
    if isinstance(value, list):
        return [t for v in value for t in _own_text(v, key)]
    return [value] if isinstance(value, str) else []


def test_no_result_claims_a_project_is_safe_or_fully_compatible(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    limited = world_of(GATEWAY)
    limited.rate_limit_paths.add("/search/repositories")
    results = [
        run(make_engine(world).find_reusable(request("AgentOps-AI/tokencost", "BerriAI/litellm"))),  # reuse
        run(make_engine(world_of(GATEWAY)).find_reusable(request("BerriAI/litellm"))),  # reuse_partial
        run(make_engine(limited).find_reusable(request(search_queries=["llm"]))),  # inconclusive
        run(make_engine(world_of(GATEWAY)).find_reusable(request(search_queries=["zzz"]))),  # no_candidates
    ]
    assert [r["verdict"] for r in results] == ["reuse", "reuse_partial", "inconclusive", "no_candidates"]
    for out in results:
        assert out["limits"] == LIMITS and "not that a project is safe or compatible" in out["limits"]
        for text in _own_text({k: v for k, v in out.items() if k != "limits"}):
            assert not _CLAIM.search(text), text
    reuse = results[0]["recommendation"]["reason"]
    assert "no gate check found a blocking issue" in reuse and "cover this task" not in reuse
    # every gate of the recommended pick passed and it scores well: still no claim beyond the checks
    top = results[0]["picks"][0]
    assert top["scores"]["total"] > 0.7 and all(top["gates"].values())


def test_instructions_and_skill_forbid_safety_claims_and_explain_the_decision() -> None:
    skill = discovery_workflow()
    assert "Never tell the user a project is safe" in SERVER_INSTRUCTIONS
    assert "`inconclusive` is not `no_candidates`" in SERVER_INSTRUCTIONS
    assert not _CLAIM.search(SERVER_INSTRUCTIONS.replace("is safe, secure or fully compatible because", ""))
    # the user-visible explanation: what was understood, required, constrained, searched and shortlisted
    for heading in (
        "**Interpreted job**",
        "**Must-have requirements**",
        "**Constraints**",
        "**Searches used**",
        "**Candidate shortlist**",
    ):
        assert heading in skill, heading
    assert "chain-of-thought" in skill
    for verdict in ("reuse_partial", "inconclusive", "no_candidates", "build_yourself"):
        assert f"**{verdict}**" in skill
    assert "never say a project is safe" in skill.lower()


def test_skill_covers_the_steps_around_the_tool_call() -> None:
    skill = discovery_workflow()
    # before: do not go looking for a dependency the project already has
    assert "Start with what the project already has" in skill
    # phrasing: the skill tells the agent how matching works, so capabilities are written to be found
    assert "one statement" in skill and "Word forms and casing already match" in skill
    # after: a verdict is evidence from a README, so it is confirmed before anything is built on it
    assert "### 8. Confirm before you build on it" in skill and "It has not been run" in skill
    assert "`coverage.found_in`" in skill
    assert "### 9. After integrating" in skill
    # the worked example passes only must-haves, and says why the fourth capability is left out
    assert "The fourth is left out of the call" in skill
