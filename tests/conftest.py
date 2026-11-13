"""A fake GitHub REST API (httpx.MockTransport) and engine factory for offline tests."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from urllib.parse import unquote

import httpx
import pytest

from reuse_layer.config import Secret, Settings
from reuse_layer.core.engine import Engine
from reuse_layer.core.models import Repo
from reuse_layer.core.store import Store
from reuse_layer.sources.github import GitHubClient, GitHubSource
from reuse_layer.sources.packages import PackageChecker, RegistryClient

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
TOKEN = "github_pat_TESTTOKEN_abcdefghijklmnopqrstuvwxyz0123"
T = TypeVar("T")


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class FakeRepo:
    full_name: str
    description: str
    topics: list[str] = field(default_factory=list)
    language: str | None = "Python"
    license: str | None = "MIT"
    created_days: float = 900
    pushed_days: float = 3
    archived: bool = False
    fork: bool = False
    private: bool = False
    owner_type: str = "Organization"
    org_verified: bool = False
    stars: int = 500
    forks: int = 50
    readme: str = ""
    releases: int = 6  # stable releases in the last year (one every ~45 days)
    # Named releases instead, newest first: (tag, prerelease). Overrides `releases`.
    release_tags: list[tuple[str, bool]] | None = None
    tags: list[str] = field(default_factory=list)  # what the /tags endpoint lists (tags without a release)
    files: dict[str, str] = field(default_factory=dict)  # path -> text at every commit (contents, trees)
    contributors: int = 20
    capped: bool = False
    health: int = 80
    ci: str = "success"
    ci_pages: list[str] | None = None  # one check-run conclusion per page, to exercise pagination
    signature: str = "valid"
    annotated: bool = True
    advisories: list[dict[str, Any]] = field(default_factory=list)  # global advisory DB rows

    def json(self) -> dict[str, Any]:
        owner = self.full_name.split("/")[0]
        return {
            "full_name": self.full_name,
            "private": self.private,
            "visibility": "private" if self.private else "public",
            "description": self.description,
            "topics": self.topics,
            "language": self.language,
            "license": {"spdx_id": self.license} if self.license else None,
            "archived": self.archived,
            "disabled": False,
            "fork": self.fork,
            "is_template": False,
            "created_at": iso(self.created_days),
            "pushed_at": iso(self.pushed_days),
            "default_branch": "main",
            "owner": {"login": owner, "type": self.owner_type},
            "stargazers_count": self.stars,
            "forks_count": self.forks,
            "open_issues_count": 3,
            "homepage": f"https://{owner.lower()}.example.org",
        }

    def release_rows(self) -> list[dict[str, Any]]:
        """The /releases payload, newest first."""
        named = self.release_tags
        if named is None:
            named = [(f"v1.{i}.0", False) for i in reversed(range(self.releases))]
        return [
            {"tag_name": tag, "draft": False, "prerelease": pre, "published_at": iso(i * 45 + 5)}
            for i, (tag, pre) in enumerate(named)
        ]

    def tag_names(self) -> list[str]:
        return [r["tag_name"] for r in self.release_rows()] + self.tags


def sha_for(name: str, ref: str) -> str:
    import hashlib

    return hashlib.sha256(f"{name}@{ref}".encode()).hexdigest()[:40]


class FakeGitHub:
    def __init__(self) -> None:
        self.repos: dict[str, FakeRepo] = {}
        self.requests: list[httpx.Request] = []
        self.fail_paths: set[str] = set()  # path substrings that return HTTP 500
        self.rate_limit_paths: set[str] = set()  # path substrings that return HTTP 429 (secondary limit)
        self.forbid_paths: set[str] = set()  # path substrings that return a plain HTTP 403
        self.missing_paths: set[str] = set()  # path substrings that return HTTP 404
        self.advisory_page_size: int | None = None  # paginate /advisories with a cursor, like GitHub
        self.contents_refs: list[str] = []  # the ?ref= of every contents read

    def add(self, *repos: FakeRepo) -> None:
        for r in repos:
            self.repos[r.full_name.lower()] = r

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ routing

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = unquote(request.url.path)
        if any(f in path for f in self.fail_paths):
            return httpx.Response(500, json={"message": "boom"})
        if any(f in path for f in self.rate_limit_paths):
            return httpx.Response(429, json={"message": "slow down"}, headers={"retry-after": "30"})
        if any(f in path for f in self.forbid_paths):  # refused, and not because of a rate limit
            return httpx.Response(403, json={"message": "Forbidden"}, headers={"x-ratelimit-remaining": "57"})
        if any(f in path for f in self.missing_paths):
            return httpx.Response(404, json={"message": "Not Found"})
        headers = {
            "x-ratelimit-resource": "search" if path.startswith("/search") else "core",
            "x-ratelimit-remaining": "4000",
            "x-ratelimit-reset": "9999999999",
        }
        if path == "/search/repositories":
            return httpx.Response(
                200, json={"items": self._search(request.url.params.get("q", ""))}, headers=headers
            )
        if path == "/advisories":
            affects = request.url.params.get("affects", "").lower()
            want_malware = request.url.params.get("type") == "malware"
            rows = [
                a
                for r in self.repos.values()
                for a in r.advisories
                if r.full_name.split("/")[1].lower() == affects
                and (a.get("type") == "malware") == want_malware
            ]
            size = self.advisory_page_size
            if size is not None:
                start = int(request.url.params.get("after", "0"))
                if start + size < len(rows):
                    # like GitHub: the next page is named by a cursor in the Link header
                    headers["link"] = f'<https://api.github.com/advisories?after={start + size}>; rel="next"'
                rows = rows[start : start + size]
            return httpx.Response(200, json=rows, headers=headers)
        if path.startswith("/orgs/"):
            org = path.split("/")[2].lower()
            verified = any(
                r.full_name.lower().startswith(org + "/") and r.org_verified for r in self.repos.values()
            )
            return httpx.Response(200, json={"login": org, "is_verified": verified}, headers=headers)
        m = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", path)
        if not m:
            return httpx.Response(404, json={"message": "Not Found"})
        repo = self.repos.get(f"{m.group(1)}/{m.group(2)}".lower())
        if repo is None or repo.private:
            return httpx.Response(404, json={"message": "Not Found"})
        return self._repo_route(repo, m.group(3) or "", request, headers)

    def _search(self, q: str) -> list[dict[str, Any]]:
        words = [w.strip('"').lower() for w in q.split() if ":" not in w and w not in ("AND", "OR", "NOT")]
        out = []
        for r in self.repos.values():
            blob = f"{r.full_name} {r.description} {' '.join(r.topics)}".lower().replace("-", " ")
            if any(w and w in blob for w in words):
                out.append(r.json())
        return out

    def _repo_route(
        self, r: FakeRepo, rest: str, request: httpx.Request, headers: dict[str, str]
    ) -> httpx.Response:
        name = r.full_name
        if rest == "":
            return httpx.Response(200, json=r.json(), headers=headers)
        if rest == "/readme":
            return httpx.Response(200, text=r.readme or f"# {name}\n\n{r.description}\n", headers=headers)
        if rest == "/releases":
            return httpx.Response(200, json=r.release_rows(), headers=headers)
        if rest == "/tags":
            return httpx.Response(200, json=[{"name": t} for t in r.tags], headers=headers)
        if rest == "/contents":  # the root directory at ?ref=
            self.contents_refs.append(request.url.params.get("ref", ""))
            top = [{"name": n, "path": n, "type": "file"} for n in r.files if "/" not in n]
            top_dirs = sorted({n.split("/")[0] for n in r.files if "/" in n} | {"src"})
            return httpx.Response(
                200, json=[*top, *({"name": d, "path": d, "type": "dir"} for d in top_dirs)], headers=headers
            )
        if rest.startswith("/contents/") and rest[len("/contents/") :] in r.files:
            self.contents_refs.append(request.url.params.get("ref", ""))
            return httpx.Response(200, text=r.files[rest[len("/contents/") :]], headers=headers)
        if rest.startswith("/git/trees/"):  # ?recursive=1: every file, plus the directories
            self.contents_refs.append(rest[len("/git/trees/") :])
            subdirs = {"/".join(n.split("/")[:i]) for n in r.files for i in range(1, n.count("/") + 1)}
            tree = [{"path": n, "type": "blob", "mode": "100644"} for n in sorted(r.files)]
            tree += [{"path": d, "type": "tree", "mode": "040000"} for d in sorted(subdirs)]
            return httpx.Response(200, json={"sha": "t", "tree": tree, "truncated": False}, headers=headers)
        if rest == "/contributors":
            rows = [
                {"login": f"dev{i}", "type": "User", "contributions": 100 - i} for i in range(r.contributors)
            ]
            h = dict(headers)
            if r.capped:
                h["link"] = '<https://api.github.com/x?page=2>; rel="next"'
            return httpx.Response(200, json=rows, headers=h)
        if rest == "/community/profile":
            return httpx.Response(200, json={"health_percentage": r.health}, headers=headers)
        if rest == "/security-advisories":
            return httpx.Response(200, json=[], headers=headers)
        m = re.match(r"^/git/ref/tags/(.+)$", rest)
        if m:
            tag = m.group(1)
            obj_type = "tag" if r.annotated else "commit"
            sha = sha_for(name, "tagobj:" + tag) if r.annotated else sha_for(name, tag)
            return httpx.Response(200, json={"object": {"type": obj_type, "sha": sha}}, headers=headers)
        m = re.match(r"^/git/tags/(.+)$", rest)
        if m:
            tag = next(t for t in r.tag_names() if sha_for(name, f"tagobj:{t}") == m.group(1))
            return httpx.Response(
                200,
                json={
                    "object": {"type": "commit", "sha": sha_for(name, tag)},
                    "verification": {"verified": r.signature == "valid", "reason": r.signature},
                },
                headers=headers,
            )
        m = re.match(r"^/git/commits/(.+)$", rest)
        if m:
            tag_sha = m.group(1)
            commit_days = next(
                (i * 45 + 5 for i, t in enumerate(r.tag_names()) if sha_for(name, t) == tag_sha),
                r.pushed_days,
            )
            return httpx.Response(
                200,
                json={
                    "verification": {"verified": r.signature == "valid", "reason": r.signature},
                    "committer": {"date": iso(commit_days)},
                },
                headers=headers,
            )
        m = re.match(r"^/branches/(.+)$", rest)
        if m:
            return httpx.Response(
                200,
                json={
                    "commit": {
                        "sha": sha_for(name, "main"),
                        "commit": {
                            "verification": {"verified": False, "reason": "unsigned"},
                            "committer": {"date": iso(r.pushed_days)},
                        },
                    }
                },
                headers=headers,
            )
        m = re.match(r"^/commits/([0-9a-f]{40})/check-runs$", rest)
        if m and r.ci_pages is not None:
            page = int(request.url.params.get("page", "1"))
            h = dict(headers)
            if page < len(r.ci_pages):
                h["link"] = (
                    f'<https://api.github.com/repositories/1/commits/x/check-runs?page={page + 1}>; rel="next"'
                )
            runs = [{"status": "completed", "conclusion": r.ci_pages[page - 1]}]
            return httpx.Response(200, json={"total_count": len(r.ci_pages), "check_runs": runs}, headers=h)
        if m:
            runs = [] if r.ci == "none" else [{"status": "completed", "conclusion": r.ci}]
            return httpx.Response(200, json={"total_count": len(runs), "check_runs": runs}, headers=headers)
        return httpx.Response(404, json={"message": "Not Found"})


class FakeRegistries:
    """Package registries (PyPI, npm, ...): exact URL -> (status, JSON, or text for an XML document).
    Anything else is a 404."""

    def __init__(self) -> None:
        self.docs: dict[str, tuple[int, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.delay_s = 0.0  # answer this slowly, to exercise the time budget

    def add(self, url: str, body: Any, status: int = 200) -> None:
        self.docs[url] = (status, body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        status, body = self.docs.get(str(request.url), (404, {"message": "not found"}))
        if status in (301, 302) and isinstance(body, str):
            return httpx.Response(status, headers={"location": body})
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers={"content-type": "application/xml"})
        return httpx.Response(status, json=body)


def pricing_world() -> FakeGitHub:
    """The LLM-cost-estimation scenario from the discovery skill, plus traps for every gate."""
    gh = FakeGitHub()
    gh.add(
        FakeRepo(
            "AgentOps-AI/tokencost",
            "Easy token price estimates for 400+ LLMs. TokenOps",
            topics=["llm", "cost-estimation", "tokens", "openai", "anthropic"],
            org_verified=True,
            forks=120,
            contributors=25,
            created_days=900,
            readme="# tokencost\n\nClient-side token counting and price estimation for LLM apps.\n\n## Features\n"
            "- Count tokens for prompts and completions\n- Calculate the cost of LLM API calls\n"
            "- Up-to-date per-token prices for 400+ models across providers\n",
        ),
        FakeRepo(
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
        ),
        FakeRepo(
            "openai/tiktoken",
            "tiktoken is a fast BPE tokeniser for use with OpenAI's models.",
            org_verified=True,
            contributors=30,
            forks=900,
            created_days=1300,
            releases=3,
            readme="# tiktoken\n\nA fast BPE tokeniser.\n\n## Features\n- Count tokens for a prompt\n- Encode and decode text\n",
        ),
        FakeRepo(
            "quantlab/optionpricer",
            "Option pricing models: Black-Scholes, binomial trees and Monte Carlo cost of carry",
            topics=["option-pricing", "black-scholes", "finance"],
            owner_type="User",
            contributors=4,
            readme="# optionpricer\n\nOption pricing model library.\n\n- Black-Scholes pricing model\n",
        ),
        FakeRepo(
            "evil-org/tokencosts",
            "Token price estimates for LLMs (fast!)",
            topics=["llm", "cost-estimation"],
            created_days=40,
            contributors=1,
            forks=1,
            stars=3000,
            owner_type="User",
            releases=1,
        ),
        FakeRepo(
            "gplco/llm-price-tracker",
            "Track LLM token prices and cost estimation",
            topics=["llm", "pricing"],
            license="GPL-3.0",
        ),
        FakeRepo(
            "oldco/llm-cost-calc", "LLM cost calculator for token pricing", topics=["llm"], archived=True
        ),
        FakeRepo(
            "vulnco/llm-cost-meter",
            "Meter LLM token cost estimation per request",
            topics=["llm", "cost-estimation"],
            advisories=[
                {
                    "ghsa_id": "GHSA-aaaa-bbbb-cccc",
                    "type": "reviewed",
                    "severity": "critical",
                    "summary": "Remote code execution in price loader",
                    "source_code_location": "https://github.com/vulnco/llm-cost-meter",
                    "vulnerabilities": [
                        {"package": {"name": "llm-cost-meter"}, "first_patched_version": None}
                    ],
                }
            ],
        ),
        FakeRepo(
            "freshco/llm-cost-lens",
            "LLM token cost estimation and price comparison across providers",
            topics=["llm", "cost-estimation", "pricing"],
            created_days=100,
            contributors=6,
            forks=10,
            stars=200,
            readme="# llm-cost-lens\n\n## Features\n- Per-token prices for LLM models\n- Token counting\n"
            "- Calculate cost of LLM API calls\n",
        ),
        FakeRepo("secret-co/private-llm-cost", "LLM cost estimation", private=True),
    )
    return gh


def make_settings(**overrides: Any) -> Settings:
    s = Settings(keys={"github": Secret(TOKEN)})
    return replace(s, **overrides) if overrides else s


@pytest.fixture
def world() -> FakeGitHub:
    return pricing_world()


@pytest.fixture
def make_engine() -> Callable[..., Engine]:
    def factory(
        gh: FakeGitHub,
        settings: Settings | None = None,
        *,
        token: bool = True,
        seed: int = 7,
        registries: FakeRegistries | None = None,
    ) -> Engine:
        settings = settings or make_settings()
        store = Store(":memory:")
        client = GitHubClient(
            token=Secret(TOKEN) if token else None,
            api_url="https://api.github.com",
            budget_ms=2000,
            store=store,
            transport=gh.transport(),
        )
        # Registries are always faked: a test never reaches a real one.
        reg = RegistryClient(
            store=store, budget_ms=2000, transport=(registries or FakeRegistries()).transport()
        )
        source = GitHubSource.from_settings(settings, client, PackageChecker(reg))
        return Engine(settings, store=store, sources=[source], now=lambda: NOW, seed=seed)

    return factory


def make_repo(name: str = "acme/thing", **fields: Any) -> Repo:
    """A healthy repository record; ``fields`` override any attribute."""
    base: dict[str, Any] = dict(
        full_name=name,
        html_url=f"https://github.com/{name}",
        description="",
        topics=(),
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
    )
    return Repo(**{**base, **fields})


def as_json(text: str) -> Any:
    return json.loads(text)
