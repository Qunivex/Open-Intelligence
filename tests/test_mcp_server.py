"""The MCP surface: tools, schemas (no secret fields), prompt, and a real in-memory client round trip."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from mcp import Client

from conftest import FakeGitHub, run
from reuse_layer.adapters.mcp_server import build_server, discovery_workflow
from reuse_layer.core.engine import Engine

SECRETISH = ("key", "token", "secret", "password", "credential", "auth")


def _server(world: FakeGitHub, make_engine: Callable[..., Engine]) -> Any:
    engine = make_engine(world)
    return build_server(lambda: engine)


def test_tool_set_and_schemas_have_no_secret_fields(
    world: FakeGitHub, make_engine: Callable[..., Engine]
) -> None:
    async def go() -> Any:
        async with Client(_server(world, make_engine)) as client:
            return (await client.list_tools()).tools

    tools = run(go())
    assert {t.name for t in tools} == {"find_reusable", "search", "report_outcome"}

    def walk(schema: Any) -> None:
        if isinstance(schema, dict):
            for name, sub in (schema.get("properties") or {}).items():
                assert not any(s in name.lower() for s in SECRETISH), name
                walk(sub)
            for v in schema.values():
                walk(v)
        elif isinstance(schema, list):
            for v in schema:
                walk(v)

    for t in tools:
        walk(t.input_schema)
    find = next(t for t in tools if t.name == "find_reusable")
    assert find.annotations.read_only_hint is True
    # The schema is the registry: an artifact type is offered exactly when a source serves it.
    engine = make_engine(world)
    for t in tools:
        if t.name != "report_outcome":
            prop = t.input_schema["properties"]["artifact_type"]
            assert prop.get("enum", [prop.get("const")]) == sorted(engine.sources)


def test_find_reusable_round_trip(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    args = {
        "task": "Estimate the cost of LLM API usage across providers",
        "capabilities": ["LLM cost estimation | cost tracking", "token counting | tokenizer"],
        "candidates": ["AgentOps-AI/tokencost", "not-a/real-repo"],
        "search_queries": ["llm cost estimation"],
        "avoid": ["option pricing"],
        "context": {"language": "python"},
    }

    async def go() -> Any:
        async with Client(_server(world, make_engine)) as client:
            return await client.call_tool("find_reusable", args)

    res = run(go())
    data = json.loads(res.content[0].text)
    assert data["verdict"] == "reuse"
    top = data["picks"][0]
    assert top["name"] == "AgentOps-AI/tokencost"
    assert (top["provider"], top["artifact_type"]) == ("github", "repository")
    assert (data["provider"], data["artifact_type"]) == ("github", "repository")
    assert data["unresolved"] == ["not-a/real-repo"]


def test_invalid_input_is_a_tool_error(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    async def go() -> Any:
        async with Client(_server(world, make_engine)) as client:
            return await client.call_tool(
                "report_outcome", {"recommendation_id": "nope", "repo": "a/b", "outcome": "kept"}
            )

    res = run(go())
    assert getattr(res, "is_error", getattr(res, "isError", False))


def test_internal_errors_do_not_leak(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    engine = make_engine(world)

    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("database password is hunter2")

    engine.search = boom  # type: ignore[method-assign]

    async def go() -> Any:
        async with Client(build_server(lambda: engine)) as client:
            return await client.call_tool("search", {"queries": ["llm"]})

    res = run(go())
    assert getattr(res, "is_error", getattr(res, "isError", False))
    assert "hunter2" not in res.content[0].text


def test_discovery_prompt(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    async def go() -> Any:
        async with Client(_server(world, make_engine)) as client:
            prompts = (await client.list_prompts()).prompts
            got = await client.get_prompt("discover_reusable", {"need": "a pricing model for LLM providers"})
            return prompts, got

    prompts, got = run(go())
    assert {p.name for p in prompts} == {"discover_reusable"}
    text = got.messages[0].content.text
    assert "Why not keywords" in text and "a pricing model for LLM providers" in text
    assert not discovery_workflow().startswith("---")
    assert "user-visible **discovery trace**" in text
    assert "## What can be searched" in text and "does not imply" not in text
    assert "say that this server cannot" in text


def test_unknown_artifact_type_is_a_tool_error(world: FakeGitHub, make_engine: Callable[..., Engine]) -> None:
    async def go() -> Any:
        async with Client(_server(world, make_engine)) as client:
            return await client.call_tool("search", {"queries": ["whisper"], "artifact_type": "model"})

    res = run(go())
    assert getattr(res, "is_error", getattr(res, "isError", False))
