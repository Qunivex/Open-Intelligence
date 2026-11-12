"""GitHub REST client: auth handling, privacy filtering, caching, rate limits, redirects."""

from __future__ import annotations

import httpx
import pytest

from conftest import TOKEN, run
from reuse_layer.config import Secret
from reuse_layer.core.store import Store
from reuse_layer.sources.github import GitHubClient, GitHubSource, RateLimited, parse_repo_ref


def _client(handler: httpx.MockTransport, *, token: bool = True, store: Store | None = None) -> GitHubClient:
    return GitHubClient(
        token=Secret(TOKEN) if token else None, store=store or Store(":memory:"), transport=handler
    )


@pytest.mark.parametrize(
    ("ref", "want"),
    [
        ("BerriAI/litellm", "BerriAI/litellm"),
        ("https://github.com/BerriAI/litellm", "BerriAI/litellm"),
        ("https://github.com/BerriAI/litellm.git", "BerriAI/litellm"),
        ("github.com/BerriAI/litellm/tree/main/docs", "BerriAI/litellm"),
        ("git@github.com:BerriAI/litellm.git", "BerriAI/litellm"),
        ("pkg:github/BerriAI/litellm@v1.2.3", "BerriAI/litellm"),
        ("litellm", None),
        ("../../etc/passwd", None),
        ("owner/..", None),
        ("-bad/name", None),
        ("a/b c", None),
    ],
)
def test_parse_repo_ref(ref: str, want: str | None) -> None:
    assert parse_repo_ref(ref) == want


def test_token_only_in_authorization_header() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    c = _client(httpx.MockTransport(handler))
    run(c.get("/repos/a/b"))
    req = seen[0]
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(req.url)
    assert req.headers["x-github-api-version"] == "2022-11-28"
    assert req.headers["user-agent"].startswith("reuse-layer/")


def test_debug_logs_show_api_shape_without_body_or_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG", logger="reuse_layer.sources.github")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"full_name": "a/public"}], "private_data": "do not log"})

    c = _client(httpx.MockTransport(handler))
    run(c.get("/search/repositories", params={"q": "token cost github_pat_TESTTOKEN_abcdefghijklmnop"}))

    logs = caplog.text
    assert "GitHub API request: GET /search/repositories" in logs
    assert "token cost" in logs
    assert "items=1" in logs
    assert "do not log" not in logs
    assert "github_pat_TESTTOKEN_abcdefghijklmnop" not in logs
    assert TOKEN not in logs


def test_private_items_are_dropped_before_caching() -> None:
    store = Store(":memory:")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {"full_name": "a/public", "private": False, "created_at": "2024-01-01T00:00:00Z"},
                    {"full_name": "a/secret", "private": True, "created_at": "2024-01-01T00:00:00Z"},
                ]
            },
        )

    c = _client(httpx.MockTransport(handler), store=store)
    res = run(c.get("/search/repositories", params={"q": "x"}))
    assert res is not None and [i["full_name"] for i in res.data["items"]] == ["a/public"]
    cached = store._db.execute("SELECT body FROM http_cache").fetchone()[0]
    assert "secret" not in cached


def test_etag_revalidation_uses_cache() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json={"n": 1}, headers={"etag": '"v1"'})

    c = _client(httpx.MockTransport(handler))
    first = run(c.get("/repos/a/b", ttl=0))
    second = run(c.get("/repos/a/b", ttl=0))  # stale -> conditional request -> 304
    assert first is not None and second is not None and second.data == {"n": 1}
    assert calls[1].headers["if-none-match"] == '"v1"'


def test_fresh_cache_skips_the_network() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"n": 1})

    c = _client(httpx.MockTransport(handler))
    run(c.get("/repos/a/b"))
    run(c.get("/repos/a/b"))
    assert len(calls) == 1


def test_primary_rate_limit_stops_further_calls() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={
                "x-ratelimit-resource": "core",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "9999999999",
            },
        )

    c = _client(httpx.MockTransport(handler), token=False)
    with pytest.raises(RateLimited, match="no token"):
        run(c.get("/repos/a/b"))
    with pytest.raises(RateLimited):
        run(c.get("/repos/c/d"))
    assert len(calls) == 1  # the second call never left the process


def test_secondary_rate_limit() -> None:
    c = _client(httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "30"})))
    with pytest.raises(RateLimited):
        run(c.get("/repos/a/b"))


def test_redirect_off_host_is_not_followed() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(301, headers={"location": "https://evil.example.com/steal"})

    c = _client(httpx.MockTransport(handler))
    assert run(c.get("/repos/a/b")) is None
    assert seen == ["https://api.github.com/repos/a/b"]


def test_same_host_redirect_follows_renames() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/old/name":
            return httpx.Response(301, headers={"location": "https://api.github.com/repositories/42"})
        return httpx.Response(
            200,
            json={
                "full_name": "new/name",
                "created_at": "2024-01-01T00:00:00Z",
                "owner": {"login": "new", "type": "User"},
            },
        )

    src = GitHubSource(_client(httpx.MockTransport(handler)))
    repo = run(src.resolve("old/name"))
    assert repo is not None and repo.full_name == "new/name"


def test_search_throttle_without_token() -> None:
    c = _client(httpx.MockTransport(lambda r: httpx.Response(200, json={"items": []})), token=False)
    for i in range(9):
        run(c.get("/search/repositories", params={"q": f"x{i}"}))
    with pytest.raises(RateLimited):
        run(c.get("/search/repositories", params={"q": "one-more"}))


def test_bad_paths_are_refused() -> None:
    c = _client(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ValueError):
        run(c.get("repos/a/b"))
    with pytest.raises(ValueError):
        run(c.get("/repos/../admin"))
