"""Security evidence for repositories, from two public services that need no account.

* **OpenSSF Scorecard** (api.scorecard.dev): an automated weekly review of a repository's security
  practices, scored 0 to 10 over checks such as Maintained, Code-Review and Dangerous-Workflow.
* **deps.dev** (api.deps.dev, Google's Open Source Insights): the packages a repository publishes,
  their current version, and the known vulnerabilities that affect that version.

These are plain HTTPS APIs, not MCP servers: neither project offers one. Only a candidate's public
repository name and package name are sent. Both lookups can be switched off under ``[evidence]``
in the config file.

A repository is checked through the package it publishes, because that is what a user installs:
"does a known vulnerability affect the version I would get today?" Finding that package is the
uncertain step, so the result always names the package and version that were checked.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import anyio
from mcp.shared._httpx_utils import create_mcp_http_client

from open_intelligence.config import EvidenceSettings
from open_intelligence.ranking.models import Advisory, Candidate, SecurityEvidence

# url -> (HTTP status, parsed JSON or None). Status 0 means the service could not be reached.
Fetch = Callable[[str], Awaitable[tuple[int, Any]]]

MAX_ADVISORIES = 8
CONCURRENCY = 6
UNCHECKED = ", so known vulnerabilities were not checked"

# Where a repository in a given language usually publishes, under its own name.
_REGISTRY = {
    "python": "pypi",
    "jupyter notebook": "pypi",
    "javascript": "npm",
    "typescript": "npm",
    "vue": "npm",
    "rust": "cargo",
    "ruby": "rubygems",
    "c#": "nuget",
}


def _normalize(name: str) -> str:
    """Package names compare equal across '-', '_' and '.': 'scikit_learn' is 'scikit-learn'."""
    artifact = name.split(":")[-1]  # Maven "group:artifact"
    return re.sub(r"[-_.]+", "-", artifact.lower())


def likely_packages(repo_id: str, language: str | None) -> list[tuple[str, str]]:
    """The packages a repository most plausibly publishes, to be verified before they are trusted."""
    name = repo_id.rsplit("/", 1)[-1]
    low = (language or "").lower()
    if low == "go":
        return [("go", f"github.com/{repo_id}")]
    if low in _REGISTRY:
        return [(_REGISTRY[low], name)]
    return [("pypi", name), ("npm", name)]  # language unknown or not tied to one registry


def listed_package(versions: Any, repo_id: str) -> tuple[str, str] | None:
    """Fallback: pick from deps.dev's list of packages whose metadata points at the repository.

    That list includes mirrors and look-alikes (anyone can claim a repository in package
    metadata), and for large repositories it is cut off before the real package appears. So the
    choice is conservative: a registry package named exactly like the repository, else one with a
    real release history, else nothing.
    """
    counts: dict[tuple[str, str], int] = {}
    for entry in versions if isinstance(versions, list) else []:
        key = entry.get("versionKey") if isinstance(entry, dict) else None
        if not isinstance(key, dict) or entry.get("relationType") != "SOURCE_REPO":
            continue
        system, name = str(key.get("system", "")), str(key.get("name", ""))
        if system and name and system != "GO":
            counts[(system, name)] = counts.get((system, name), 0) + 1
    target = _normalize(repo_id.rsplit("/", 1)[-1])
    same_name = [k for k in counts if _normalize(k[1]) == target and "/" not in k[1]]
    if same_name:
        return max(same_name, key=lambda k: counts[k])
    if counts:
        best = max(counts, key=lambda k: counts[k])
        if counts[best] >= 5:
            return best
    return None


def _links_to(version: dict[str, Any], repo_id: str) -> bool:
    """Does this package version name the repository as its source?"""
    wanted = f"github.com/{repo_id}".lower()
    for project in version.get("relatedProjects") or []:
        key = project.get("projectKey") if isinstance(project, dict) else None
        if isinstance(key, dict) and str(key.get("id", "")).lower() == wanted:
            return True
    for link in version.get("links") or []:
        url = str(link.get("url", "")).lower().rstrip("/") if isinstance(link, dict) else ""
        if url.removesuffix(".git").endswith(wanted):
            return True
    return False


class SecurityLookup:
    def __init__(self, settings: EvidenceSettings | None = None, fetch: Fetch | None = None) -> None:
        self.settings = settings or EvidenceSettings()
        self._fetch = fetch

    @property
    def enabled(self) -> bool:
        return self.settings.scorecard or self.settings.advisories

    async def lookup_many(self, candidates: list[Candidate]) -> None:
        """Set ``security`` on each repository. Never raises: what failed is written in its notes."""
        todo = [c for c in candidates if c.source == "github" and c.security is None]
        if not todo or not self.enabled:
            return
        limiter = anyio.CapacityLimiter(CONCURRENCY)

        async def run(fetch: Fetch) -> None:
            async def one(c: Candidate) -> None:
                async with limiter:
                    try:
                        c.security = await self.lookup(fetch, c.id, c.programming_language)
                    except Exception:
                        c.security = SecurityEvidence(notes=["the security lookup failed"])

            with anyio.move_on_after(self.settings.timeout_s):
                async with anyio.create_task_group() as tg:
                    for c in todo:
                        tg.start_soon(one, c)
            for c in todo:
                if c.security is None:
                    c.security = SecurityEvidence(notes=["the security lookup timed out"])

        if self._fetch is not None:
            await run(self._fetch)
            return
        async with create_mcp_http_client() as http:

            async def fetch(url: str) -> tuple[int, Any]:
                try:
                    response = await http.get(url)
                except Exception:
                    return 0, None
                try:
                    return response.status_code, response.json()
                except ValueError:
                    return response.status_code, None

            await run(fetch)

    async def lookup(self, fetch: Fetch, repo_id: str, language: str | None = None) -> SecurityEvidence:
        evidence = SecurityEvidence()
        if self.settings.scorecard:
            await self._scorecard(fetch, repo_id, evidence)
        if self.settings.advisories:
            await self._advisories(fetch, repo_id, language, evidence)
        return evidence

    async def _scorecard(self, fetch: Fetch, repo_id: str, evidence: SecurityEvidence) -> None:
        owner, _, repo = repo_id.partition("/")
        status, data = await fetch(
            f"{self.settings.scorecard_url}/projects/github.com/{quote(owner)}/{quote(repo)}"
        )
        if status == 200 and isinstance(data, dict) and isinstance(data.get("score"), int | float):
            if data["score"] >= 0:
                evidence.scorecard = float(data["score"])
                evidence.scorecard_date = str(data.get("date") or "")[:10] or None
            for check in data.get("checks") or []:
                if isinstance(check, dict) and isinstance(check.get("score"), int | float):
                    evidence.scorecard_checks[str(check.get("name"))] = int(
                        check["score"]
                    )  # -1: inconclusive
        elif status == 404:
            evidence.notes.append("not covered by OpenSSF Scorecard")
        else:
            evidence.notes.append("OpenSSF Scorecard could not be reached")

    async def _advisories(
        self, fetch: Fetch, repo_id: str, language: str | None, evidence: SecurityEvidence
    ) -> None:
        base = f"{self.settings.deps_dev_url}/v3"
        # 1. The package named like the repository, accepted only if it names this repository as its source.
        for system, name in likely_packages(repo_id, language):
            if await self._check_package(fetch, base, system, name, repo_id, evidence):
                return
        # 2. Otherwise, what deps.dev itself lists for the repository.
        project = quote(f"github.com/{repo_id}", safe="")
        status, data = await fetch(f"{base}/projects/{project}:packageversions")
        if status not in (200, 404):
            evidence.notes.append("deps.dev could not be reached" + UNCHECKED)
            return
        package = listed_package(data.get("versions") if isinstance(data, dict) else None, repo_id)
        if package is None or not await self._check_package(fetch, base, *package, None, evidence):
            evidence.notes.append("no published package was found for it" + UNCHECKED)

    async def _check_package(
        self,
        fetch: Fetch,
        base: str,
        system: str,
        name: str,
        must_link_to: str | None,
        evidence: SecurityEvidence,
    ) -> bool:
        """Read the current version of a package and its advisories. False when it is not the repository's."""
        package_url = f"{base}/systems/{system.lower()}/packages/{quote(name, safe='')}"
        status, listing = await fetch(package_url)
        versions = listing.get("versions") if status == 200 and isinstance(listing, dict) else None
        if not isinstance(versions, list) or not versions:
            return False
        current = next((v for v in versions if isinstance(v, dict) and v.get("isDefault")), versions[-1])
        version = str((current.get("versionKey") or {}).get("version", ""))
        status, detail = await fetch(f"{package_url}/versions/{quote(version, safe='')}")
        if status != 200 or not isinstance(detail, dict):
            return False
        if must_link_to is not None and not _links_to(detail, must_link_to):
            return False
        evidence.package, evidence.package_version = f"{system.lower()}:{name}", version
        ids = [
            str(k.get("id")) for k in detail.get("advisoryKeys") or [] if isinstance(k, dict) and k.get("id")
        ]
        found: list[Advisory] = []
        known: set[str] = set()  # ids and aliases already listed: GHSA and PYSEC ids often name one flaw
        for advisory_id in ids[:MAX_ADVISORIES]:
            if advisory_id in known:
                continue
            status, info = await fetch(f"{base}/advisories/{quote(advisory_id, safe='')}")
            info = info if status == 200 and isinstance(info, dict) else {}
            aliases = {str(a) for a in info.get("aliases") or []}
            if aliases & known:
                continue
            known |= aliases | {advisory_id}
            score = info.get("cvss3Score")
            # deps.dev reports 0 when an advisory has no CVSS score.
            cvss = float(score) if isinstance(score, int | float) and score > 0 else None
            found.append(Advisory(id=advisory_id, title=str(info.get("title") or "")[:160], cvss=cvss))
        evidence.advisories = found
        return True
