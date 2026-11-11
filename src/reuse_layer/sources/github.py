"""GitHub source plugin: search, resolve and enrich public repositories through the REST API.

Security properties:

* The token travels only in the ``Authorization`` header to the configured
  API host. Redirects are followed manually and only to the same host, so the
  header never leaves GitHub. The token is never put in a URL, a cache key, a
  log line or a tool response.
* Only public repositories are returned: every search carries ``is:public``
  and any row marked private is dropped before it is cached.
* Every call has a time budget; rate limits (primary and secondary) stop
  further calls instead of hammering the API; ETags make repeat reads free.
* Owner and repo names are validated and URL-quoted before use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit

import httpx

from reuse_layer import __version__
from reuse_layer.config import Secret
from reuse_layer.core.models import Advisory, CiState, DeepFacts, Pin, Release, Repo, utcnow
from reuse_layer.core.store import CachedResponse, Store
from reuse_layer.core.text import sanitize

log = logging.getLogger(__name__)

API_VERSION = "2022-11-28"
JSON = "application/vnd.github+json"
RAW = "application/vnd.github.raw+json"

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_NAME = r"[A-Za-z0-9._-]{1,100}"
REPO_RE = re.compile(rf"^{_OWNER}/{_NAME}$")
_LOG_SECRET = re.compile(
    r"(?i)\b(?:github_pat_[A-Za-z0-9_]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|sk-ant-[A-Za-z0-9_-]{12,})\b"
)

HOUR = 3600
DAY = 86400


class RateLimited(Exception):
    """GitHub asked us to stop (primary or secondary rate limit)."""


class GitHubError(Exception):
    """GitHub answered with an unexpected error."""


def parse_repo_ref(ref: str) -> str | None:
    """Normalise ``owner/repo``, a github.com URL, an SSH remote or ``pkg:github/...`` to ``owner/repo``."""
    s = (ref or "").strip()
    s = re.sub(r"^pkg:github/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^git@github\.com:", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^(?:git\+)?(?:https?://)?(?:www\.)?github\.com/", "", s, flags=re.IGNORECASE)
    s = s.split("#", 1)[0].split("?", 1)[0]
    parts = [p for p in s.split("/") if p]
    if len(parts) < 2:
        return None
    owner, name = parts[0], parts[1].split("@", 1)[0]
    if name.lower().endswith(".git"):
        name = name[:-4]
    candidate = f"{owner}/{name}"
    if name in {".", ".."} or not REPO_RE.match(candidate):
        return None
    return candidate


def repo_url_key(url: str | None) -> str | None:
    """Comparable form of a repository URL: ``github.com/owner/repo`` lower-cased."""
    if not url:
        return None
    ref = parse_repo_ref(url)
    return f"github.com/{ref.lower()}" if ref else None


# --------------------------------------------------------------------------- HTTP client


@dataclass(frozen=True)
class GHResponse:
    status: int
    data: Any
    link: str | None
    from_cache: bool

    @property
    def has_next(self) -> bool:
        return bool(self.link and 'rel="next"' in self.link)


@dataclass
class RateState:
    remaining: dict[str, int]
    reset_at: dict[str, float]

    def blocked(self, resource: str) -> bool:
        return self.remaining.get(resource, 1) <= 0 and self.reset_at.get(resource, 0) > time.time()


class GitHubClient:
    def __init__(
        self,
        *,
        token: Secret | None,
        api_url: str = "https://api.github.com",
        budget_ms: int = 2000,
        store: Store | None = None,
        max_concurrency: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._api = urlsplit(api_url)
        self._store = store
        self._sem = asyncio.Semaphore(max_concurrency)
        self._search_times: deque[float] = deque()
        self.rate = RateState(remaining={}, reset_at={})
        self.calls = 0
        self.cache_hits = 0
        self._client = httpx.AsyncClient(
            base_url=api_url,
            transport=transport,
            timeout=httpx.Timeout(budget_ms / 1000),
            follow_redirects=False,
            headers={"User-Agent": f"reuse-layer/{__version__}", "X-GitHub-Api-Version": API_VERSION},
        )

    @property
    def authenticated(self) -> bool:
        return self._token is not None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        accept: str = JSON,
        ttl: int = 6 * HOUR,
        max_bytes: int = 2_000_000,
    ) -> GHResponse | None:
        """GET ``path``. None for not found / unavailable. Raises RateLimited or GitHubError."""
        if not path.startswith("/") or ".." in path:
            raise ValueError("invalid API path")
        resource = "search" if path.startswith("/search/") else "core"
        key = f"GET {path}?{urlencode(sorted((params or {}).items()))} {accept}"
        cached = self._store.cache_get(key) if self._store else None
        if cached is not None and time.time() - cached.fetched_at < ttl:
            self.cache_hits += 1
            log.debug(
                "GitHub API cache hit: GET %s params=%s (%s)",
                path,
                _redact_log_params(params),
                _response_shape(json.loads(cached.body)),
            )
            return self._from_cache(cached)
        if self.rate.blocked(resource):
            raise RateLimited(f"GitHub {resource} rate limit reached")
        if resource == "search":
            self._throttle_search()

        log.debug("GitHub API request: GET %s params=%s", path, _redact_log_params(params))
        headers = {"Accept": accept}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.reveal()}"
        if cached is not None and cached.etag:
            headers["If-None-Match"] = cached.etag

        url = path
        request_started = time.monotonic()
        for _ in range(3):  # follow at most two same-host redirects (renamed or transferred repos)
            async with self._sem:
                self.calls += 1
                try:
                    resp = await self._client.get(url, params=None if "?" in url else params, headers=headers)
                except httpx.TimeoutException as exc:
                    log.warning(
                        "GitHub API timeout: GET %s elapsed_ms=%d",
                        path,
                        int((time.monotonic() - request_started) * 1000),
                    )
                    raise GitHubError("GitHub did not answer within the time budget") from exc
                except httpx.HTTPError as exc:
                    log.warning(
                        "GitHub API transport error: GET %s error=%s elapsed_ms=%d",
                        path,
                        type(exc).__name__,
                        int((time.monotonic() - request_started) * 1000),
                    )
                    raise GitHubError(f"GitHub request failed: {type(exc).__name__}") from exc
            self._note_rate(resp)
            if resp.status_code in (301, 302, 307, 308):
                loc = urlsplit(resp.headers.get("location", ""))
                if loc.scheme != "https" or loc.netloc != self._api.netloc:
                    return None  # never follow (and never send the token) off the API host
                url = loc.path + (f"?{loc.query}" if loc.query else "")
                continue
            break
        else:
            return None

        status = resp.status_code
        if status == 304 and cached is not None and self._store is not None:
            self._store.cache_touch(key)
            self.cache_hits += 1
            log.debug(
                "GitHub API response: GET %s status=304 cache=validated elapsed_ms=%d",
                path,
                int((time.monotonic() - request_started) * 1000),
            )
            return self._from_cache(cached)
        if status in (403, 429) and self._is_rate_limited(resp):
            log.warning("GitHub API rate limited: GET %s status=%d", path, status)
            raise RateLimited(
                "GitHub rate limit reached" + ("" if self.authenticated else " (no token configured)")
            )
        if status in (204, 404, 409, 410, 451) or (status == 403 and resource == "core"):
            log.debug("GitHub API response: GET %s status=%d result=unavailable", path, status)
            # Not found, empty, gone, blocked, or forbidden for this resource: cache briefly as a miss.
            if self._store is not None:
                self._store.cache_put(key, 404, None, "null", None)
            return None
        if status == 202:
            log.debug("GitHub API response: GET %s status=202 result=pending", path)
            return None  # GitHub is computing statistics; treat as unavailable
        if status == 401:
            log.error("GitHub API rejected configured credentials: GET %s status=401", path)
            raise GitHubError("GitHub rejected the token (401); check keys.github in the config file")
        if status >= 400:
            log.warning("GitHub API error: GET %s status=%d", path, status)
            raise GitHubError(f"GitHub returned HTTP {status}")

        body = resp.content[:max_bytes].decode("utf-8", errors="replace")
        if accept == RAW:
            data: Any = body
            stored = json.dumps(body)
        else:
            try:
                data = json.loads(body)
            except json.JSONDecodeError as exc:
                raise GitHubError("GitHub returned a malformed response") from exc
            data = _drop_private(data)
            if data is None:
                return None
            stored = json.dumps(data)
        link = resp.headers.get("link")
        if self._store is not None:
            self._store.cache_put(key, status, resp.headers.get("etag"), stored, link)
        log.debug(
            "GitHub API response: GET %s status=%d cache=miss elapsed_ms=%d %s",
            path,
            status,
            int((time.monotonic() - request_started) * 1000),
            _response_shape(data),
        )
        return GHResponse(status=status, data=data, link=link, from_cache=False)

    @staticmethod
    def _from_cache(c: CachedResponse) -> GHResponse | None:
        if c.status == 404:
            return None
        return GHResponse(status=c.status, data=json.loads(c.body), link=c.link, from_cache=True)

    def _note_rate(self, resp: httpx.Response) -> None:
        res = resp.headers.get("x-ratelimit-resource")
        rem = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        if res and rem is not None and rem.isdigit():
            self.rate.remaining[res] = int(rem)
            if reset and reset.isdigit():
                self.rate.reset_at[res] = float(reset)

    @staticmethod
    def _is_rate_limited(resp: httpx.Response) -> bool:
        if resp.status_code == 429 or resp.headers.get("retry-after"):
            return True
        if resp.headers.get("x-ratelimit-remaining") == "0":
            return True
        try:
            msg = str(resp.json().get("message", "")).lower()
        except (ValueError, AttributeError):
            msg = ""
        return "rate limit" in msg

    def _throttle_search(self) -> None:
        """Stay under the search API's per-minute limit (30 with a token, 10 without)."""
        limit = 28 if self.authenticated else 9
        now = time.time()
        while self._search_times and now - self._search_times[0] > 60:
            self._search_times.popleft()
        if len(self._search_times) >= limit:
            raise RateLimited("GitHub search limit for this minute reached")
        self._search_times.append(now)


def _drop_private(data: Any) -> Any:
    """Remove anything private before it is cached or returned."""
    if isinstance(data, dict):
        if data.get("private") is True or data.get("visibility") in ("private", "internal"):
            return None
        if isinstance(data.get("items"), list):
            data = dict(data)
            data["items"] = [i for i in data["items"] if _drop_private(i) is not None]
    elif isinstance(data, list):
        return [i for i in data if not (isinstance(i, dict) and _drop_private(i) is None)]
    return data


def _redact_log_params(params: Mapping[str, str | int] | None) -> dict[str, str | int]:
    if not params:
        return {}
    return {
        key: (
            "********"
            if any(word in key.lower() for word in ("token", "key", "secret", "password"))
            else _redact_log_text(value)
            if isinstance(value, str)
            else value
        )
        for key, value in params.items()
    }


def _redact_log_text(value: str) -> str:
    return _LOG_SECRET.sub("********", value)


def _response_shape(data: Any) -> str:
    if isinstance(data, dict):
        collections = [(key, value) for key, value in data.items() if isinstance(value, list)]
        if collections:
            return ", ".join(f"{key}={len(value)}" for key, value in collections)
        return f"object_keys={len(data)}"
    if isinstance(data, list):
        return f"items={len(data)}"
    if isinstance(data, str):
        return f"text_chars={len(data)}"
    return f"data_type={type(data).__name__}"


# --------------------------------------------------------------------------- parsing


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def repo_from_json(d: Any) -> Repo | None:
    if not isinstance(d, dict) or _drop_private(d) is None:
        return None
    full_name = d.get("full_name")
    if not isinstance(full_name, str) or not REPO_RE.match(full_name):
        return None
    created = _dt(d.get("created_at"))
    if created is None:
        return None
    owner: dict[str, Any] = d["owner"] if isinstance(d.get("owner"), dict) else {}
    lic: dict[str, Any] = d["license"] if isinstance(d.get("license"), dict) else {}
    topics: list[Any] = d["topics"] if isinstance(d.get("topics"), list) else []
    homepage = d.get("homepage")
    return Repo(
        full_name=full_name,
        html_url=f"https://github.com/{full_name}",
        description=sanitize(d.get("description"), 300),
        topics=tuple(sanitize(t, 50) for t in topics[:20] if isinstance(t, str)),
        language=sanitize(d.get("language"), 40) or None,
        license_spdx=lic.get("spdx_id") if isinstance(lic.get("spdx_id"), str) else None,
        archived=bool(d.get("archived")),
        disabled=bool(d.get("disabled")),
        fork=bool(d.get("fork")),
        is_template=bool(d.get("is_template")),
        created_at=created,
        pushed_at=_dt(d.get("pushed_at")),
        default_branch=str(d.get("default_branch") or "main"),
        owner_login=str(owner.get("login") or full_name.split("/")[0]),
        owner_type=str(owner.get("type") or "User"),
        stars=_int(d.get("stargazers_count")),
        forks=_int(d.get("forks_count")),
        open_issues=_int(d.get("open_issues_count")),
        homepage=sanitize(homepage, 200)
        if isinstance(homepage, str) and homepage.startswith("https://")
        else None,
    )


_SEMVER = re.compile(r"^[^\d]*?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_PRE = re.compile(r"(alpha|beta|rc|pre|dev|nightly|canary|snapshot)", re.IGNORECASE)


def version_key(tag: str) -> tuple[int, int, int] | None:
    m = _SEMVER.match(tag)
    if not m or _PRE.search(tag):
        return None
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


# --------------------------------------------------------------------------- source plugin


class GitHubSource:
    """The only source in v1. GitHub is an open host (design tier 3): candidates
    earn trust from evidence and owner curation, never from being on GitHub."""

    name = "github"
    kinds = frozenset({"project"})
    tier: Literal[3] = 3

    def __init__(self, client: GitHubClient) -> None:
        self.client = client

    @staticmethod
    def _path(full_name: str, *rest: str) -> str:
        owner, name = full_name.split("/", 1)
        tail = "".join("/" + quote(r, safe="") for r in rest)
        return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}{tail}"

    async def search(self, query: str, per_page: int = 15) -> list[Repo]:
        res = await self.client.get(
            "/search/repositories", params={"q": query, "per_page": per_page}, ttl=HOUR
        )
        items = res.data.get("items") if res is not None and isinstance(res.data, dict) else None
        repos = [r for r in (repo_from_json(i) for i in (items or [])) if r is not None]
        log.debug(
            "GitHub repository search parsed: query=%r returned=%d repos=%s",
            _redact_log_text(query),
            len(repos),
            [repo.full_name for repo in repos],
        )
        return repos

    async def resolve(self, ref: str) -> Repo | None:
        """Exact repository, or nothing. Follows renames to the canonical name."""
        full = parse_repo_ref(ref)
        if full is None:
            return None
        res = await self.client.get(self._path(full), ttl=6 * HOUR)
        return repo_from_json(res.data) if res is not None else None

    async def enrich(self, repo: Repo) -> DeepFacts:
        facts = DeepFacts()
        fn = repo.full_name

        async def safe(name: str, coro: Any) -> Any:
            try:
                return await coro
            except RateLimited:
                facts.errors.append(f"{name}: rate limited")
                raise
            except GitHubError as exc:
                facts.errors.append(f"{name}: {exc}")
                return _FAILED

        jobs: dict[str, Any] = {
            "readme": self.client.get(self._path(fn, "readme"), accept=RAW, ttl=DAY, max_bytes=300_000),
            "releases": self.client.get(self._path(fn, "releases"), params={"per_page": 30}, ttl=6 * HOUR),
            "contributors": self.client.get(
                self._path(fn, "contributors"), params={"per_page": 100}, ttl=DAY
            ),
            "community": self.client.get(self._path(fn, "community", "profile"), ttl=DAY),
            "repo_advisories": self.client.get(
                self._path(fn, "security-advisories"),
                params={"state": "published", "per_page": 50},
                ttl=6 * HOUR,
            ),
            "advisories": self.client.get(
                "/advisories", params={"affects": repo.name, "per_page": 100}, ttl=6 * HOUR
            ),
            "malware": self.client.get(
                "/advisories", params={"affects": repo.name, "type": "malware", "per_page": 100}, ttl=6 * HOUR
            ),
        }
        if repo.owner_type == "Organization":
            jobs["org"] = self.client.get(f"/orgs/{quote(repo.owner_login, safe='')}", ttl=DAY)
        names = list(jobs)
        results = await asyncio.gather(*(safe(n, jobs[n]) for n in names), return_exceptions=True)
        got: dict[str, Any] = {}
        for n, r in zip(names, results, strict=True):
            if isinstance(r, RateLimited):
                raise r
            if isinstance(r, BaseException):
                facts.errors.append(f"{n}: {type(r).__name__}")
                got[n] = _FAILED
            else:
                got[n] = r

        readme = got["readme"]
        if isinstance(readme, GHResponse) and isinstance(readme.data, str):
            facts.readme = readme.data
        facts.releases = _releases(got["releases"])
        _contributors(got["contributors"], facts)
        community = got["community"]
        if isinstance(community, GHResponse) and isinstance(community.data, dict):
            hp = community.data.get("health_percentage")
            facts.health_pct = hp if isinstance(hp, int) else None
        if "org" in got and isinstance(got["org"], GHResponse) and isinstance(got["org"].data, dict):
            facts.org_verified = bool(got["org"].data.get("is_verified"))
        _advisories(repo, got, facts)

        facts.pin = await self._pin(repo, facts)
        if facts.pin is not None:
            facts.ci = await self._ci(repo, facts.pin.sha, facts)
        return facts

    async def _pin(self, repo: Repo, facts: DeepFacts) -> Pin | None:
        """Pin to the latest stable release tag (else the highest version tag, else the default branch)."""
        ref: str | None = None
        kind: Literal["release", "tag", "branch"] = "release"
        stable = [r for r in facts.releases if not r.prerelease]
        if stable:
            ref = stable[0].tag
        else:
            try:
                tags = await self.client.get(
                    self._path(repo.full_name, "tags"), params={"per_page": 50}, ttl=6 * HOUR
                )
            except GitHubError as exc:
                facts.errors.append(f"tags: {exc}")
                tags = None
            names = [
                t.get("name")
                for t in (tags.data if tags and isinstance(tags.data, list) else [])
                if isinstance(t, dict)
            ]
            versioned = [(version_key(n), n) for n in names if isinstance(n, str) and version_key(n)]
            if versioned:
                ref, kind = max(versioned)[1], "tag"

        try:
            branch = await self.client.get(
                self._path(repo.full_name, "branches", repo.default_branch), ttl=HOUR
            )
        except GitHubError as exc:
            facts.errors.append(f"branch freshness: {exc}")
            branch = None
        branch_commit = branch.data.get("commit") if branch and isinstance(branch.data, dict) else None
        branch_commit_data = branch_commit.get("commit") if isinstance(branch_commit, dict) else None
        if isinstance(branch_commit_data, dict):
            committer = branch_commit_data.get("committer")
            author = branch_commit_data.get("author")
            facts.default_branch_commit_at = _dt(
                (committer if isinstance(committer, dict) else {}).get("date")
                or (author if isinstance(author, dict) else {}).get("date")
            )

        if ref is not None:
            return await self._pin_tag(repo, ref, kind, facts)
        if not isinstance(branch_commit, dict):
            facts.errors.append("pin: default branch head could not be resolved")
            return None
        sha = branch_commit.get("sha")
        verification = (
            branch_commit_data.get("verification") if isinstance(branch_commit_data, dict) else None
        ) or {}
        if not isinstance(verification, dict):
            verification = {}
        if not isinstance(sha, str):
            return None
        return Pin(
            ref=repo.default_branch,
            kind="branch",
            sha=sha,
            signature=_signature(verification),
            committed_at=facts.default_branch_commit_at,
        )

    async def _pin_tag(
        self, repo: Repo, tag: str, kind: Literal["release", "tag", "branch"], facts: DeepFacts
    ) -> Pin | None:
        ref = await self.client.get(self._path(repo.full_name, "git", "ref", "tags", tag), ttl=6 * HOUR)
        obj = ref.data.get("object") if ref is not None and isinstance(ref.data, dict) else None
        if not isinstance(obj, dict) or not isinstance(obj.get("sha"), str):
            return None
        if obj.get("type") == "tag":  # annotated tag: its own signature, pointing at a commit
            tag_obj = await self.client.get(self._path(repo.full_name, "git", "tags", obj["sha"]), ttl=DAY)
            if tag_obj is None or not isinstance(tag_obj.data, dict):
                return None
            target = tag_obj.data.get("object") or {}
            sha = target.get("sha")
            sig = _signature(tag_obj.data.get("verification") or {})
            committed_at = None
            if isinstance(sha, str):
                try:
                    commit_sig, committed_at = await self._commit_metadata(repo, sha)
                    if sig == "unsigned":
                        sig = commit_sig
                except GitHubError as exc:
                    if sig == "unsigned":
                        raise
                    facts.errors.append(f"pin commit date: {exc}")
        else:
            sha = obj["sha"]
            sig, committed_at = await self._commit_metadata(repo, sha)
        if not isinstance(sha, str):
            return None
        return Pin(ref=tag, kind=kind, sha=sha, signature=sig, committed_at=committed_at)

    async def _commit_metadata(self, repo: Repo, sha: str) -> tuple[str, datetime | None]:
        c = await self.client.get(self._path(repo.full_name, "git", "commits", sha), ttl=DAY)
        if c is None or not isinstance(c.data, dict):
            return "unknown", None
        committer = c.data.get("committer")
        author = c.data.get("author")
        committed_at = _dt(
            (committer if isinstance(committer, dict) else {}).get("date")
            or (author if isinstance(author, dict) else {}).get("date")
        )
        return _signature(c.data.get("verification") or {}), committed_at

    async def _ci(self, repo: Repo, sha: str, facts: DeepFacts) -> CiState:
        try:
            res = await self.client.get(
                self._path(repo.full_name, "commits", sha, "check-runs"),
                params={"per_page": 50},
                ttl=6 * HOUR,
            )
        except GitHubError as exc:
            facts.errors.append(f"checks: {exc}")
            return "unknown"
        if res is None or not isinstance(res.data, dict):
            return "unknown"
        runs = [r for r in res.data.get("check_runs") or [] if isinstance(r, dict)]
        if not runs:
            return "none"
        conclusions = [r.get("conclusion") for r in runs]
        if any(c in ("failure", "timed_out", "startup_failure", "action_required") for c in conclusions):
            return "failing"
        if any(r.get("status") != "completed" for r in runs):
            return "pending"
        if all(c in ("success", "neutral", "skipped") for c in conclusions):
            return "passing"
        return "unknown"

    async def rate_limit(self) -> dict[str, Any] | None:
        """Current limits (does not count against them). Used by `reuse-layer doctor`."""
        res = await self.client.get("/rate_limit", ttl=0)
        return res.data if res is not None and isinstance(res.data, dict) else None


_FAILED = object()


def _signature(verification: Mapping[str, Any]) -> str:
    if verification.get("verified") is True:
        return "valid"
    reason = verification.get("reason")
    return str(reason) if isinstance(reason, str) and reason else "unsigned"


def _releases(res: Any) -> list[Release]:
    if not isinstance(res, GHResponse) or not isinstance(res.data, list):
        return []
    out = []
    for r in res.data:
        if not isinstance(r, dict) or r.get("draft") or not isinstance(r.get("tag_name"), str):
            continue
        out.append(
            Release(
                tag=r["tag_name"],
                published_at=_dt(r.get("published_at")),
                prerelease=bool(r.get("prerelease")),
            )
        )
    return out


def _contributors(res: Any, facts: DeepFacts) -> None:
    if not isinstance(res, GHResponse) or not isinstance(res.data, list):
        return
    raw = [c.get("contributions") for c in res.data if isinstance(c, dict) and c.get("type") != "Bot"]
    counts: list[int] = [c for c in raw if isinstance(c, int) and c > 0]
    facts.contributors = len(counts)
    facts.contributors_capped = res.has_next
    total = sum(counts)
    facts.top_share = (max(counts) / total) if total else None


def _advisories(repo: Repo, got: dict[str, Any], facts: DeepFacts) -> None:
    sources = (got["repo_advisories"], got["advisories"], got["malware"])
    facts.advisories_checked = all(s is not _FAILED for s in sources)
    me = repo_url_key(repo.html_url)
    seen: set[str] = set()

    repo_adv = got["repo_advisories"]
    for a in repo_adv.data if isinstance(repo_adv, GHResponse) and isinstance(repo_adv.data, list) else []:
        if not isinstance(a, dict) or a.get("withdrawn_at") or a.get("state") not in (None, "published"):
            continue
        vulns = [v for v in a.get("vulnerabilities") or [] if isinstance(v, dict)]
        patched = None if not vulns else all(bool(v.get("patched_versions")) for v in vulns)
        _add(facts, seen, a, patched, malware=False, source="repository")

    for key, malware in (("advisories", False), ("malware", True)):
        res = got[key]
        for a in res.data if isinstance(res, GHResponse) and isinstance(res.data, list) else []:
            if not isinstance(a, dict) or a.get("withdrawn_at"):
                continue
            if repo_url_key(a.get("source_code_location")) != me:
                continue  # an advisory for a different project with the same package name
            vulns = [v for v in a.get("vulnerabilities") or [] if isinstance(v, dict)]
            patched = None if not vulns else all(bool(v.get("first_patched_version")) for v in vulns)
            _add(
                facts,
                seen,
                a,
                patched,
                malware=malware or a.get("type") == "malware",
                source="github-advisory-database",
            )


def _add(
    facts: DeepFacts, seen: set[str], a: dict[str, Any], patched: bool | None, *, malware: bool, source: str
) -> None:
    ghsa = a.get("ghsa_id")
    if not isinstance(ghsa, str) or ghsa in seen:
        return
    seen.add(ghsa)
    sev = a.get("severity") if a.get("severity") in ("low", "medium", "high", "critical") else "unknown"
    facts.advisories.append(
        Advisory(
            ghsa_id=sanitize(ghsa, 40),
            severity=str(sev),
            summary=sanitize(a.get("summary"), 160),
            patched=patched,
            malware=malware,
            source=source,
        )
    )


def recent(dt: datetime | None, days: int, now: datetime | None = None) -> bool:
    return dt is not None and (now or utcnow()) - dt <= timedelta(days=days)
