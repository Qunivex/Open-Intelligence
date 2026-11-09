"""End-to-end find_reusable / search / report_outcome against the fake GitHub API."""

from __future__ import annotations

import json
from collections.abc import Callable

from conftest import TOKEN, FakeGitHub, FakeRepo, run
from reuse_layer.core.engine import Engine, compact_json
from reuse_layer.core.models import CallerContext, FindRequest, OutcomeReport

PRICING = FindRequest(
    task="Estimate and compare the cost of LLM API usage across providers from token counts and per-token prices",
    capabilities=[
        "up-to-date per-token prices for many LLM models | LLM pricing data | model prices",
        "count tokens for prompts and completions | token counting | tokenizer",
        "calculate cost of LLM API calls | LLM cost estimation | cost tracking",
    ],
    candidates=["AgentOps-AI/tokencost", "BerriAI/litellm", "openai/tiktoken", "made-up/llm-price-oracle"],
    search_queries=["llm cost estimation", "option pricing model", "token price"],
    avoid=["option pricing", "black-scholes"],
    context=CallerContext(language="python"),
)


def test_pricing_scenario_picks_the_right_projects(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    out = run(make_engine(world).find_reusable(PRICING))
    assert out["verdict"] == "reuse"
    names = [p["repo"].removeprefix("https://github.com/") for p in out["picks"]]
    assert 1 <= len(names) <= 3
    assert names[0] == "AgentOps-AI/tokencost"
    assert "quantlab/optionpricer" not in names  # same words, different meaning
    assert out["unresolved"] == ["made-up/llm-price-oracle"]  # invented names never become picks


def test_every_gate_trap_is_rejected_with_a_reason(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    out = run(make_engine(world).find_reusable(PRICING))
    rejected = {r["repo"]: (r["gate"], r["reason"]) for r in out["rejected"]}
    assert rejected["gplco/llm-price-tracker"][0] == "license"
    assert rejected["oldco/llm-cost-calc"][0] == "status"
    assert rejected["vulnco/llm-cost-meter"][0] == "vulns"
    assert "GHSA-aaaa-bbbb-cccc" in rejected["vulnco/llm-cost-meter"][1]
    assert rejected["evil-org/tokencosts"][0] == "lookalike"
    all_names = json.dumps(out)
    assert "private-llm-cost" not in all_names  # private repos never surface


def test_picks_are_pinned_with_evidence(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    out = run(make_engine(world).find_reusable(PRICING))
    top = out["picks"][0]
    assert top["id"].startswith("pkg:github/AgentOps-AI/tokencost@v1.")
    assert len(top["pin"]["commit"]) == 40
    assert top["pin"]["commit"] in top["archive"]
    assert "--branch v1." in top["get"]
    assert top["gates"]["license"].startswith("MIT")
    assert top["gates"]["vulns"] == "no published advisories"
    assert "signed, verified by GitHub" in top["gates"]["provenance"]
    assert any("capabilities" in w for w in top["why"])
    assert set(top["scores"]) == {"total", "relevance", "trust", "preference", "verified", "tier"}
    assert top["source"] == {"name": "GitHub", "tier": 2}  # verified organization


def test_at_most_one_newer_project_takes_the_explore_slot(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    out = run(make_engine(world).find_reusable(PRICING))
    newer = [p for p in out["picks"] if p["tier"] == "newer"]
    assert len(newer) <= 1
    for p in newer:
        assert p.get("explore") is True


def test_response_is_compact(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    out = run(make_engine(world).find_reusable(PRICING))
    # ~1,500 tokens is roughly 6,000 characters of compact JSON
    assert len(compact_json(out)) < 6500


def test_token_never_leaks_into_urls_or_responses(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    out = run(make_engine(world).find_reusable(PRICING))
    assert TOKEN not in compact_json(out)
    for req in world.requests:
        assert TOKEN not in str(req.url)
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        assert req.url.host == "api.github.com"


def test_unconnected_github_is_reported_not_asked_for(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    out = run(make_engine(world, token=False).find_reusable(PRICING))
    assert any("not connected" in n and "never ask for the token in chat" in n for n in out["notes"])
    assert all("authorization" not in r.headers for r in world.requests)


def test_build_yourself_when_nothing_fits(make_engine: Callable[..., Engine]) -> None:
    gh = FakeGitHub()
    gh.add(FakeRepo("acme/hud-kit", "Generic dashboard widgets", topics=["dashboard"]))
    req = FindRequest(
        task="Spaceship cockpit HUD with flight controls for our WebXR game",
        capabilities=["cockpit HUD overlay | heads-up display", "flight controls | joystick input"],
        candidates=["acme/hud-kit"],
    )
    out = run(make_engine(gh).find_reusable(req))
    assert out["verdict"] == "build_yourself"
    assert out["build_yourself"]["recommended"] is True


def test_no_candidates_when_everything_fails_a_gate(make_engine: Callable[..., Engine]) -> None:
    gh = FakeGitHub()
    gh.add(FakeRepo("gpl/only", "LLM cost estimation", topics=["llm"], license="AGPL-3.0"))
    out = run(make_engine(gh).find_reusable(FindRequest(task="LLM cost estimation", candidates=["gpl/only"])))
    assert out["verdict"] == "no_candidates"
    assert out["picks"] == []
    assert out["rejected"][0]["gate"] == "license"


def test_failed_vulnerability_check_fails_closed(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    world.fail_paths.add("/security-advisories")
    out = run(make_engine(world).find_reusable(PRICING))
    assert out["picks"] == []
    assert out["verdict"] == "no_candidates"
    assert any("fails closed" in r["reason"] for r in out["rejected"])


def test_language_gate_and_license_override(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    world.add(
        FakeRepo("rusty/llm-cost-rs", "LLM token cost estimation in Rust", topics=["llm"], language="Rust")
    )
    req = PRICING.model_copy(
        update={
            "candidates": ["rusty/llm-cost-rs", "gplco/llm-price-tracker"],
            "context": CallerContext(language="python", license_policy="any_osi"),
        }
    )
    out = run(make_engine(world).find_reusable(req))
    rejected = {r["repo"]: r["gate"] for r in out["rejected"]}
    assert rejected.get("rusty/llm-cost-rs") == "fit"
    assert "gplco/llm-price-tracker" not in rejected  # GPL allowed under any_osi


def test_fallback_queries_when_caller_gives_none(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    req = FindRequest(
        task="I want to build a pricing model that helps people understand what would be the cost "
        "of implementation using different LLM providers"
    )
    out = run(make_engine(world).find_reusable(req))
    assert any("fallback" in n for n in out["notes"])
    for q in out["queries"]:
        for generic in ("want", "build", "helps", "people", "implementation", "different"):
            assert generic not in q.split()


def test_search_github_is_compact_and_public_only(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    out = run(make_engine(world).search(["llm cost estimation is:private user:@me"], per_query=5))
    q = out["results"][0]["query"]
    assert "is:private" not in q and "@me" not in q and "is:public" in q
    repos = out["results"][0]["repos"]
    assert 0 < len(repos) <= 5
    assert all("private" not in r["repo"] for r in repos)
    assert "untrusted" in out


def test_report_outcome_feeds_preference(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    engine = make_engine(world)
    out = run(engine.find_reusable(PRICING))
    rec = out["recommendation_id"]
    top = out["picks"][0]["repo"].removeprefix("https://github.com/")

    bad = engine.report_outcome(OutcomeReport(recommendation_id="rec_" + "0" * 26, repo=top, outcome="kept"))
    assert bad == {"accepted": False, "reason": "unknown recommendation_id"}
    not_pick = engine.report_outcome(
        OutcomeReport(recommendation_id=rec, repo="quantlab/optionpricer", outcome="kept")
    )
    assert not_pick["accepted"] is False

    ok = engine.report_outcome(OutcomeReport(recommendation_id=rec, repo=top, outcome="kept"))
    assert ok["accepted"] is True and ok["retention"] > 0.5
    dup = engine.report_outcome(OutcomeReport(recommendation_id=rec, repo=top, outcome="kept"))
    assert dup["accepted"] is False

    again = run(engine.find_reusable(PRICING))
    top_again = next(p for p in again["picks"] if p["repo"].endswith(top))
    assert top_again["scores"]["preference"] > 0.5
    assert any("recorded integrations" in w for w in top_again["why"])


def test_task_text_is_not_retained_by_default(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    engine = make_engine(world)
    run(engine.find_reusable(PRICING))
    rows = engine.store._db.execute("SELECT task_text, task_hash FROM recommendation").fetchall()
    assert rows and rows[0][0] is None and len(rows[0][1]) == 64
