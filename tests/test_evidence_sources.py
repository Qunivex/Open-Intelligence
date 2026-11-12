"""What the layer concludes when GitHub refuses, truncates or errors, instead of answering.

Each of these used to end in a cleaner result than the evidence supported: a refused advisory check read as
"no published advisories", the first page of a long list read as the whole list, the first fifty check runs
read as "CI passing".
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest

from conftest import TOKEN, FakeGitHub, FakeRepo, make_settings, run
from reuse_layer.config import Secret
from reuse_layer.core.engine import Engine
from reuse_layer.core.models import CallerContext, FindRequest
from reuse_layer.core.store import Store
from reuse_layer.sources.github import GitHubClient, GitHubError, _next_query

KIT = FakeRepo(
    "solo/llm-cost-kit",
    "LLM token cost estimation",
    topics=["llm", "cost-estimation"],
    readme="# llm-cost-kit\n\n## Features\n- Estimate the cost of LLM API calls\n",
)
REQUEST = FindRequest(
    task="LLM cost estimation",
    capabilities=["LLM cost estimation | cost tracking"],
    candidates=["solo/llm-cost-kit"],
    context=CallerContext(language="python"),
)


def advisory(n: int, severity: str = "low", *, fixed: bool = True, located: bool = True) -> dict[str, Any]:
    return {
        "ghsa_id": f"GHSA-0000-0000-{n:04d}",
        "type": "reviewed",
        "severity": severity,
        "summary": f"finding {n}",
        "source_code_location": "https://github.com/solo/llm-cost-kit" if located else None,
        "vulnerabilities": [
            {"package": {"name": "llm-cost-kit"}, "first_patched_version": "1.2.3" if fixed else None}
        ],
    }


def find(gh: FakeGitHub, make_engine: Callable[..., Engine]) -> dict[str, Any]:
    return run(make_engine(gh).find_reusable(REQUEST))


def world(repo: FakeRepo = KIT) -> FakeGitHub:
    gh = FakeGitHub()
    gh.add(repo)
    return gh


# --------------------------------------------------------------------------- refused is not empty


@pytest.mark.parametrize("path", ["/security-advisories", "/advisories"])
@pytest.mark.parametrize("answer", ["forbid_paths", "missing_paths"])
def test_an_advisory_source_that_does_not_answer_is_not_a_clean_result(
    path: str, answer: str, make_engine: Callable[..., Engine]
) -> None:
    gh = world()
    getattr(gh, answer).add(path)  # GitHub says 403 or 404 instead of returning a list
    out = find(gh, make_engine)
    assert out["picks"] == []  # was: a `reuse` pick whose vulns gate read "no published advisories"
    (rejected,) = out["rejected"]
    assert rejected["gate"] == "vulns" and rejected["unchecked"] is True
    assert out["verdict"] == "inconclusive"


def _client(handler: Callable[[httpx.Request], httpx.Response], store: Store) -> GitHubClient:
    return GitHubClient(token=Secret(TOKEN), store=store, transport=httpx.MockTransport(handler))


def test_forbidden_is_an_error_and_is_never_cached_as_a_miss() -> None:
    store = Store(":memory:")
    answers = [httpx.Response(403, json={"message": "Forbidden"}), httpx.Response(200, json={"ok": True})]
    client = _client(lambda req: answers.pop(0), store)
    with pytest.raises(GitHubError, match="403"):
        run(client.get("/repos/a/b/contributors"))
    assert store.stats()["cached_responses"] == 0  # a refusal must not hide the next, successful, answer
    again = run(client.get("/repos/a/b/contributors"))
    assert again is not None and again.data == {"ok": True}


def test_a_refused_candidate_is_unverified_not_invented(make_engine: Callable[..., Engine]) -> None:
    gh = world()
    gh.forbid_paths.add("/repos/solo/llm-cost-kit")
    out = find(gh, make_engine)
    assert "unresolved" not in out  # that list is for names GitHub says do not exist
    assert "candidate not verified (GitHub error): solo/llm-cost-kit" in out["unresolved_checks"]
    assert out["verdict"] == "inconclusive"


def test_a_refused_contributor_list_is_reported(make_engine: Callable[..., Engine]) -> None:
    gh = world()
    gh.forbid_paths.add("/contributors")  # what GitHub answers for very large repositories
    out = find(gh, make_engine)
    assert out["verdict"] == "reuse" and out["evidence"]["status"] == "partial"
    assert out["picks"][0]["unresolved_checks"] == ["contributor data not read"]


# --------------------------------------------------------------------------- the whole list, or not checked


def test_next_query_reads_only_the_parameters_of_the_next_page() -> None:
    link = (
        '<https://api.github.com/advisories?affects=x&after=Y3Vyc29y>; rel="next", '
        '<https://api.github.com/advisories?affects=x&before=abc>; rel="prev"'
    )
    assert _next_query(link) == {"affects": "x", "after": "Y3Vyc29y"}
    assert _next_query('<https://evil.example.com/steal?page=2>; rel="next"') == {"page": "2"}  # host ignored
    assert _next_query('<https://api.github.com/x?page=9>; rel="last"') == {}
    assert _next_query(None) == {}


def test_an_advisory_on_a_later_page_is_found(make_engine: Callable[..., Engine]) -> None:
    rows = [advisory(1), advisory(2), advisory(3, "critical", fixed=False)]
    gh = world(replace(KIT, advisories=rows))
    gh.advisory_page_size = 1  # the blocking advisory is on page three
    out = find(gh, make_engine)
    assert out["picks"] == []  # was: a pick, because only the first page was read
    (rejected,) = out["rejected"]
    assert rejected["gate"] == "vulns" and "GHSA-0000-0000-0003 (critical)" in rejected["reason"]
    assert out["verdict"] == "no_candidates"  # a finding, on evidence that was read to the end
    for req in gh.requests:  # every page was fetched from the API host, on the path the layer chose
        assert req.url.host == "api.github.com"


def test_a_paginated_list_that_is_read_to_the_end_counts_as_checked(
    make_engine: Callable[..., Engine],
) -> None:
    gh = world(replace(KIT, advisories=[advisory(n) for n in range(1, 5)]))
    gh.advisory_page_size = 1
    out = find(gh, make_engine)
    assert out["verdict"] == "reuse"
    assert out["picks"][0]["gates"]["vulns"] == "4 published advisories, all with fixes"


def test_an_advisory_list_too_long_to_finish_fails_closed(make_engine: Callable[..., Engine]) -> None:
    gh = world(replace(KIT, advisories=[advisory(n) for n in range(1, 8)]))  # seven pages; five are read
    gh.advisory_page_size = 1
    out = find(gh, make_engine)
    assert out["picks"] == []
    (rejected,) = out["rejected"]
    assert rejected["unchecked"] is True and "could not complete" in rejected["reason"]
    assert out["verdict"] == "inconclusive"


def test_advisories_that_name_no_repository_are_pointed_out(make_engine: Callable[..., Engine]) -> None:
    gh = world(replace(KIT, advisories=[advisory(1, "critical", fixed=False, located=False)]))
    out = find(gh, make_engine)
    pick = out["picks"][0]  # it cannot be pinned on this project, so it does not fail the gate...
    assert pick["gates"]["vulns"] == "no published advisories"
    assert pick["unresolved_checks"] == [  # ...and it cannot be ruled out, so it is not passed over either
        "1 advisories for a package with this name cite no repository: not counted against it, review them"
    ]


# --------------------------------------------------------------------------- CI: "passing" needs every run


def _verified(repo: FakeRepo, make_engine: Callable[..., Engine]) -> tuple[float, dict[str, Any]]:
    pick = find(world(repo), make_engine)["picks"][0]
    return pick["scores"]["verified"], pick


def test_a_failing_check_run_on_a_later_page_is_seen(make_engine: Callable[..., Engine]) -> None:
    score, pick = _verified(replace(KIT, ci_pages=["success", "failure"]), make_engine)
    assert score == 0.0  # was 1.0: "upstream CI passing", from the first page alone
    assert "Upstream CI is failing on the pinned commit" in pick["integration_risks"]


def test_ci_is_passing_when_every_page_passes(make_engine: Callable[..., Engine]) -> None:
    score, pick = _verified(replace(KIT, ci_pages=["success", "success", "success"]), make_engine)
    assert score == 1.0 and "Upstream CI passing on the pinned commit" in pick["why"]
    assert "unresolved_checks" not in pick


def test_ci_is_not_called_passing_when_runs_were_left_unread(make_engine: Callable[..., Engine]) -> None:
    score, pick = _verified(replace(KIT, ci_pages=["success"] * 5), make_engine)  # five pages; three are read
    assert score == 0.4 and "CI status unknown" in pick["why"]
    assert pick["unresolved_checks"] == [
        "upstream CI has more check runs than were read, so it is not reported as passing"
    ]


# --------------------------------------------------------------------------- one failed lookup, one lost fact


def test_a_tag_lookup_error_costs_the_pin_not_the_whole_evidence_pass(
    make_engine: Callable[..., Engine],
) -> None:
    gh = world()
    gh.fail_paths.add("/git/ref/tags")
    out = find(gh, make_engine)
    (rejected,) = out["rejected"]
    # was: gate "evidence", "evidence checks did not run", with everything else about the project lost
    assert rejected["gate"] == "provenance" and rejected["unchecked"] is True
    assert out["evidence"]["deep_checked"] == 1


def test_unreadable_release_history_is_not_reported_as_no_releases(
    make_engine: Callable[..., Engine],
) -> None:
    gh = world()
    gh.fail_paths.add("/releases")
    pick = find(gh, make_engine)["picks"][0]
    assert pick["pin"]["ref"] == "main"  # the release could not be found, so the branch head was pinned
    risks = " | ".join(pick["integration_risks"])
    assert "Release history could not be read: pinned to branch main" in risks
    assert "No releases or version tags" not in risks  # that would be a claim the evidence does not support
    assert "releases not listed: the pin may not be the latest release" in pick["unresolved_checks"]


# --------------------------------------------------------------------------- housekeeping


def test_old_cache_entries_are_dropped_when_the_engine_starts() -> None:
    store = Store(":memory:")
    store.cache_put("GET /old", 200, None, '"x"', None)
    store.cache_put("GET /fresh", 200, None, '"y"', None)
    store._db.execute(
        "UPDATE http_cache SET fetched_at = ? WHERE key = ?", (time.time() - 30 * 86400, "GET /old")
    )
    engine = Engine.create(make_settings(), transport=FakeGitHub().transport(), store=store)
    run(engine.aclose())
    assert store.cache_get("GET /old") is None and store.cache_get("GET /fresh") is not None
