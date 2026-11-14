"""Security evidence against responses recorded from api.scorecard.dev and api.deps.dev (pallets/flask)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from open_intelligence.config import EvidenceSettings
from open_intelligence.ranking.evidence import SecurityLookup, likely_packages, listed_package
from open_intelligence.ranking.models import Candidate

pytestmark = pytest.mark.anyio

RECORDED: dict[str, list[Any]] = json.loads(
    (Path(__file__).with_name("fixtures") / "security_api.json").read_text(encoding="utf-8")
)
PACKAGE = "https://api.deps.dev/v3/systems/pypi/packages/flask"


def fetcher(responses: dict[str, list[Any]], seen: list[str] | None = None) -> Any:
    async def fetch(url: str) -> tuple[int, Any]:
        if seen is not None:
            seen.append(url)
        status, data = responses.get(url, [404, None])
        return status, data

    return fetch


def with_old_release_current() -> dict[str, list[Any]]:
    """The recording, changed in one way: 2.2.2 (which has known vulnerabilities) is the current release."""
    responses = copy.deepcopy(RECORDED)
    for version in responses[PACKAGE][1]["versions"]:
        version["isDefault"] = version["versionKey"]["version"] == "2.2.2"
    return responses


async def test_scorecard_and_a_clean_current_release() -> None:
    evidence = await SecurityLookup().lookup(fetcher(RECORDED), "pallets/flask", "Python")
    assert evidence.scorecard == pytest.approx(7.1)
    assert evidence.scorecard_date == "2026-11-09"
    assert evidence.scorecard_checks["Maintained"] == 10
    assert evidence.package == "pypi:flask"
    assert evidence.package_version == "3.1.3"
    assert evidence.advisories == []  # checked, and none affect the current release
    assert evidence.notes == []


async def test_advisories_of_the_current_release_are_listed_once_each() -> None:
    evidence = await SecurityLookup().lookup(fetcher(with_old_release_current()), "pallets/flask", "Python")
    assert evidence.package_version == "2.2.2"
    advisories = {a.id: a for a in evidence.advisories or []}
    # deps.dev lists GHSA and PYSEC ids for the same flaw: aliases are folded together.
    assert "GHSA-m2qf-hxjv-5gpq" in advisories
    assert "PYSEC-2023-62" not in advisories
    assert advisories["GHSA-m2qf-hxjv-5gpq"].cvss == pytest.approx(7.5)
    assert advisories["GHSA-m2qf-hxjv-5gpq"].severity == "high"
    assert len(advisories) < 4


async def test_a_same_named_package_is_not_trusted_unless_it_names_the_repository() -> None:
    responses = copy.deepcopy(RECORDED)
    # Someone else's repository, with the same name as the PyPI package "flask".
    evidence = await SecurityLookup().lookup(fetcher(responses), "example-squatter/flask", "Python")
    assert evidence.package is None
    assert evidence.advisories is None  # not checked, which is different from "none found"
    assert any("no published package was found" in note for note in evidence.notes)


async def test_unknown_repository_is_reported_not_guessed() -> None:
    evidence = await SecurityLookup().lookup(fetcher(RECORDED), "this-org-does-not-exist-xyz/nope", None)
    assert evidence.scorecard is None
    assert evidence.advisories is None
    assert "not covered by OpenSSF Scorecard" in evidence.notes


async def test_unreachable_services_are_notes_not_errors() -> None:
    async def down(url: str) -> tuple[int, Any]:
        return 0, None

    evidence = await SecurityLookup().lookup(down, "pallets/flask", "Python")
    assert evidence.scorecard is None
    assert "OpenSSF Scorecard could not be reached" in evidence.notes
    assert any("deps.dev could not be reached" in note for note in evidence.notes)


async def test_each_lookup_can_be_switched_off() -> None:
    seen: list[str] = []
    lookup = SecurityLookup(EvidenceSettings(scorecard=False, advisories=True), fetch=fetcher(RECORDED, seen))
    c = Candidate(source="github", need="code", id="pallets/flask", url="u", programming_language="Python")
    await lookup.lookup_many([c])
    assert c.security is not None
    assert c.security.scorecard is None
    assert c.security.package == "pypi:flask"
    assert not any("scorecard.dev" in url for url in seen)

    off = SecurityLookup(EvidenceSettings(scorecard=False, advisories=False), fetch=fetcher(RECORDED, seen))
    untouched = Candidate(source="github", need="code", id="pallets/flask", url="u")
    await off.lookup_many([untouched])
    assert untouched.security is None


async def test_only_repositories_are_looked_up() -> None:
    seen: list[str] = []
    model = Candidate(source="huggingface", need="model", id="pallets/flask", url="u")
    await SecurityLookup(fetch=fetcher(RECORDED, seen)).lookup_many([model])
    assert model.security is None
    assert seen == []


def test_likely_packages_follow_the_language() -> None:
    assert likely_packages("pallets/flask", "Python") == [("pypi", "flask")]
    assert likely_packages("expressjs/express", "JavaScript") == [("npm", "express")]
    assert likely_packages("gin-gonic/gin", "Go") == [("go", "github.com/gin-gonic/gin")]
    assert likely_packages("BurntSushi/ripgrep", "Rust") == [("cargo", "ripgrep")]
    assert likely_packages("someone/tool", None) == [("pypi", "tool"), ("npm", "tool")]


def test_listed_package_ignores_mirrors_and_go_proxies() -> None:
    def entry(system: str, name: str, relation: str = "SOURCE_REPO") -> dict[str, Any]:
        return {"versionKey": {"system": system, "name": name, "version": "1"}, "relationType": relation}

    # What deps.dev really lists for lodash/lodash: Go proxy copies and npm look-alikes, not "lodash".
    listing = [entry("GO", "github.com/lodash/lodash")] * 22 + [entry("NPM", "@depup/lodash")] * 3
    assert listed_package(listing, "lodash/lodash") is None
    assert listed_package([*listing, entry("NPM", "lodash")], "lodash/lodash") == ("NPM", "lodash")
    # A differently named package is accepted only with a real release history.
    assert listed_package([entry("PYPI", "torch")] * 2, "pytorch/pytorch") is None
    assert listed_package([entry("PYPI", "torch")] * 6, "pytorch/pytorch") == ("PYPI", "torch")
    assert listed_package([entry("PYPI", "flask", "ISSUE_TRACKER")] * 9, "pallets/flask") is None
    assert listed_package(None, "a/b") is None
