from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import Client

from open_intelligence.connector import Connector
from open_intelligence.server import build_server, skill_text
from tests.conftest import SECRET

pytestmark = pytest.mark.anyio


async def _call(connector: Connector, tool: str, args: dict[str, Any]) -> Any:
    async with Client(build_server(connector)) as client:
        result = await client.call_tool(tool, args)
    return result


async def test_exposes_a_small_tool_surface(connector: Connector) -> None:
    async with Client(build_server(connector)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        prompts = {p.name for p in (await client.list_prompts()).prompts}
    assert set(tools) == {
        "route_request",
        "search_open_source",
        "list_sources",
        "list_source_tools",
        "call_source_tool",
    }
    assert all(t.annotations and t.annotations.read_only_hint for t in tools.values())
    assert prompts == {"open_source_routing"}


async def test_route_request(connector: Connector) -> None:
    result = await _call(
        connector, "route_request", {"task": "Send data from Census to Snowflake with Python"}
    )
    data = result.structured_content
    assert [s["source"] for s in data["sources"]] == ["github"]


async def test_search_fans_out_to_routed_sources_only(connector: Connector) -> None:
    result = await _call(
        connector,
        "search_open_source",
        {
            "task": "Fine-tune a sentiment classifier on product reviews",
            "query": "sentiment",
            "queries": {"code": "sentiment fine-tuning"},
            "limit": 3,
        },
    )
    data = result.structured_content
    calls = {(r["source"], r["need"]): r for r in data["results"]}
    assert set(calls) == {
        ("github", "code"),
        ("huggingface", "model"),
        ("huggingface", "dataset"),
        ("kaggle", "model"),
        ("kaggle", "dataset"),
    }
    assert all(r["ok"] for r in data["results"]), data["results"]
    assert json.loads(calls[("github", "code")]["untrusted_content"])["args"] == {
        "query": "sentiment fine-tuning",
        "perPage": 3,
    }
    assert json.loads(calls[("huggingface", "dataset")]["untrusted_content"])["args"]["repo_types"] == [
        "dataset"
    ]
    kaggle_ds = json.loads(calls[("kaggle", "dataset")]["untrusted_content"])["args"]
    assert kaggle_ds == {"search": "sentiment", "pageSize": 3, "sortBy": "Relevance"}
    assert calls[("kaggle", "model")]["role"] == "secondary"
    assert "none yet" in data["ranking"]


async def test_search_with_caller_needs(connector: Connector) -> None:
    result = await _call(
        connector,
        "search_open_source",
        {"task": "Snowflake pipeline from Census", "query": "census snowflake", "needs": ["code"]},
    )
    data = result.structured_content
    assert [(r["source"], r["need"]) for r in data["results"]] == [("github", "code")]
    assert data["routing"]["decided_by"] == "caller"


async def test_search_reports_unavailable_source_inline() -> None:
    from tests.conftest import fake_settings

    connector = Connector(fake_settings(), environ={})  # no GitHub token
    result = await _call(connector, "search_open_source", {"task": "Build a RAG chatbot", "query": "rag"})
    data = result.structured_content
    github = next(r for r in data["results"] if r["source"] == "github")
    assert github["ok"] is False and "needs a token" in github["error"]
    assert any(r["ok"] for r in data["results"] if r["source"] == "huggingface")


async def test_call_source_tool_refuses_write_tools(connector: Connector) -> None:
    result = await _call(
        connector,
        "call_source_tool",
        {"source": "github", "tool": "create_issue", "arguments": {"owner": "a", "repo": "b", "title": "t"}},
    )
    assert result.structured_content["ok"] is False
    assert "not allowed" in result.structured_content["error"]


async def test_list_sources_never_shows_the_token(connector: Connector) -> None:
    result = await _call(connector, "list_sources", {"check": True})
    text = json.dumps(result.structured_content)
    assert SECRET not in text
    assert [s["source"] for s in result.structured_content["sources"]] == ["github", "huggingface", "kaggle"]


async def test_list_source_tools_returns_live_schemas(connector: Connector) -> None:
    result = await _call(connector, "list_source_tools", {"source": "huggingface"})
    tools = {t["name"]: t for t in result.structured_content["tools"]}
    assert "hub_repo_search" in tools and "query" in tools["hub_repo_search"]["input_schema"]["properties"]


def test_skill_text_is_bundled() -> None:
    text = skill_text()
    assert text.startswith("# Open-source routing")
    assert "Route by the kind of artifact" in text
