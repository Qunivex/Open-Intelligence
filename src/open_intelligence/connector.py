"""Client side of the gateway: connect to the official upstream MCP servers and call their tools.

One short-lived MCP client connection per source per request: connect, do the work, close. That
keeps the gateway stateless and means a source that is down only affects its own results.

Safety rules enforced here, not left to the agent:

* only tools in the source's ``allowed_tools`` (read-only discovery tools) can be listed or called;
* tokens come from environment variables, are sent only to their own source, and never appear in
  any status, error or result;
* upstream text is returned as data, capped at ``max_result_chars``.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

# The SDK's own factory for an HTTP client with MCP-appropriate timeouts and headers.
from mcp.shared._httpx_utils import create_mcp_http_client

from open_intelligence.config import Settings, token_environment
from open_intelligence.sources import SourceSpec

T = TypeVar("T")


class SourceError(Exception):
    """A problem worth showing to the agent as-is (no internals, no secrets)."""


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class CallOutcome:
    source: str
    tool: str
    arguments: dict[str, Any]
    ok: bool
    text: str = ""
    truncated: bool = False
    error: str | None = None
    # The upstream tool's structured result, when it sends one. For the ranking pipeline only:
    # it is not part of what the gateway returns to the agent.
    structured: Any = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
        }
        if self.ok:
            out["untrusted_content"] = self.text
            if self.truncated:
                out["truncated"] = True
        else:
            out["error"] = self.error
        return out


@dataclass
class SourceCheck:
    source: str
    enabled: bool
    transport: str
    endpoint: str
    auth: str
    connected: bool | None = None
    tools: list[str] = field(default_factory=list)
    allowed_but_not_offered: list[str] = field(default_factory=list)
    upstream_tool_count: int | None = None
    error: str | None = None
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if v not in (None, [], "")}
        out["enabled"] = self.enabled
        return out


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Connector:
    def __init__(self, settings: Settings, environ: Mapping[str, str] | None = None) -> None:
        self.settings = settings
        # Tokens come from the environment, or from a git-ignored .env file next to where this runs.
        self._environ: Mapping[str, str] = (
            environ if environ is not None else token_environment(settings.sources)
        )

    # --- configuration ------------------------------------------------------------------------

    def spec(self, source_id: str) -> SourceSpec:
        spec = self.settings.sources.get(source_id)
        if spec is None:
            raise SourceError(f"unknown source {source_id!r}; known sources: {sorted(self.settings.sources)}")
        return spec

    def _token(self, spec: SourceSpec) -> tuple[str | None, str | None]:
        for name in spec.token_env:
            value = self._environ.get(name, "").strip()
            if value:
                return value, name
        return None, None

    def auth_status(self, spec: SourceSpec) -> str:
        _, name = self._token(spec)
        if name:
            return f"token from ${name}"
        wanted = " or ".join(f"${n}" for n in spec.token_env) or "a token"
        if spec.token_required:
            return f"missing: set {wanted}"
        return f"anonymous (optional: set {wanted})"

    def unavailable_reason(self, spec: SourceSpec) -> str | None:
        """Why this source cannot be used right now, without touching the network; None if it can."""
        if not spec.enabled:
            return f"{spec.id} is disabled in the config file"
        if spec.token_required and self._token(spec)[1] is None:
            names = " or ".join(f"${n}" for n in spec.token_env)
            return f"{spec.id} needs a token: set {names} in the environment of the Open Intelligence server"
        return None

    # --- connections --------------------------------------------------------------------------

    @asynccontextmanager
    async def open(self, source_id: str) -> AsyncIterator[Client]:
        spec = self.spec(source_id)
        reason = self.unavailable_reason(spec)
        if reason:
            raise SourceError(reason)
        token, token_name = self._token(spec)

        if spec.transport == "http":
            headers = dict(spec.headers)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            http = create_mcp_http_client(headers=headers)
            async with http, Client(streamable_http_client(spec.url or "", http_client=http)) as client:
                yield client
        else:
            env = {
                k: _ENV_REF.sub(lambda m: self._environ.get(m.group(1), ""), v) for k, v in spec.env.items()
            }
            if token and token_name and spec.token_env:
                # Hand the token to the child under the first name the source lists (the name the
                # official server reads, e.g. GITHUB_PERSONAL_ACCESS_TOKEN).
                env.setdefault(spec.token_env[0], token)
            params = StdioServerParameters(command=spec.command or "", args=list(spec.args), env=env or None)
            async with Client(params) as client:
                yield client

    async def _offered_tools(self, client: Client) -> list[ToolInfo]:
        tools: list[ToolInfo] = []
        cursor: str | None = None
        while True:
            page = await client.list_tools(cursor=cursor)
            tools.extend(
                ToolInfo(
                    name=t.name, description=t.description or "", input_schema=dict(t.input_schema or {})
                )
                for t in page.tools
            )
            cursor = page.next_cursor
            if not cursor:
                return tools

    async def list_tools(self, source_id: str) -> list[ToolInfo]:
        """The source's tools that this gateway allows, with their live schemas."""
        spec = self.spec(source_id)

        async def work() -> list[ToolInfo]:
            async with self.open(source_id) as client:
                offered = await self._offered_tools(client)
            return [t for t in offered if t.name in spec.allowed_tools]

        return await self._guarded(spec, work)

    async def call(self, source_id: str, tool: str, arguments: dict[str, Any] | None = None) -> CallOutcome:
        return (await self.call_many(source_id, [(tool, arguments or {})]))[0]

    async def call_many(
        self,
        source_id: str,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        full_text: bool = False,
        concurrency: int = 1,
    ) -> list[CallOutcome]:
        """Run several tool calls on one source over a single connection.

        Outcomes come back in the order of ``calls``. ``full_text`` skips the output cap (for the
        ranking pipeline, which parses results instead of returning them). ``concurrency`` is how
        many calls may be in flight at once.
        """
        spec = self.spec(source_id)
        outcomes = [CallOutcome(source=source_id, tool=t, arguments=a, ok=False) for t, a in calls]
        blocked = [o for o in outcomes if o.tool not in spec.allowed_tools]
        for o in blocked:
            o.error = (
                f"tool {o.tool!r} is not allowed on {source_id}; allowed: {sorted(spec.allowed_tools)}. "
                "Open Intelligence only calls read-only discovery tools."
            )
        todo = [o for o in outcomes if o not in blocked]
        if not todo:
            return outcomes

        limiter = anyio.CapacityLimiter(max(1, concurrency))

        async def one(client: Client, o: CallOutcome) -> None:
            async with limiter:
                try:
                    result = await client.call_tool(o.tool, o.arguments)
                except Exception as exc:
                    o.error = describe_error(exc)
                    return
            text = _result_text(result)
            if result.is_error:
                o.error = f"{source_id} returned an error: {_cap(text, 600)[0].strip()}"
                if "unauth" in text.lower() and spec.token_env and self._token(spec)[1] is None:
                    o.error += f" (this tool needs a {source_id} token: set ${spec.token_env[0]})"
                return
            o.structured = getattr(result, "structured_content", None)
            o.text, o.truncated = (text, False) if full_text else _cap(text, self.settings.max_result_chars)
            o.ok = True

        async def work() -> None:
            async with self.open(source_id) as client, anyio.create_task_group() as tg:
                for o in todo:
                    tg.start_soon(one, client, o)

        try:
            await self._guarded(spec, work)
        except SourceError as exc:
            for o in todo:
                if not o.ok and o.error is None:
                    o.error = str(exc)
        return outcomes

    async def check(self, source_id: str, connect: bool = True) -> SourceCheck:
        spec = self.spec(source_id)
        check = SourceCheck(
            source=spec.id,
            enabled=spec.enabled,
            transport=spec.transport,
            endpoint=spec.endpoint,
            auth=self.auth_status(spec),
            notes=spec.notes,
        )
        reason = self.unavailable_reason(spec)
        if reason:
            check.connected = False
            check.error = reason
            return check
        if not connect:
            return check

        async def work() -> list[ToolInfo]:
            async with self.open(source_id) as client:
                return await self._offered_tools(client)

        try:
            offered = await self._guarded(spec, work)
        except SourceError as exc:
            check.connected = False
            check.error = str(exc)
            return check
        names = {t.name for t in offered}
        check.connected = True
        check.upstream_tool_count = len(offered)
        check.tools = sorted(names & spec.allowed_tools)
        check.allowed_but_not_offered = sorted(spec.allowed_tools - names)
        return check

    async def _guarded(self, spec: SourceSpec, work: Callable[[], Awaitable[T]]) -> T:
        """Run ``work`` with the gateway timeout; turn any failure into a clean SourceError."""
        try:
            with anyio.fail_after(self.settings.timeout_s):
                return await work()
        except SourceError:
            raise
        except TimeoutError as exc:
            raise SourceError(f"{spec.id} did not answer within {self.settings.timeout_s:.0f}s") from exc
        except Exception as exc:
            message = f"could not reach {spec.id} at {spec.endpoint}: {describe_error(exc)}"
            if spec.token_env and self._token(spec)[1] is None:
                message += f" (no token set; {spec.id} may need ${spec.token_env[0]})"
            raise SourceError(message) from exc


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for inner in exc.exceptions:
            out.extend(_leaves(inner))
        return out
    return [exc]


def describe_error(exc: BaseException) -> str:
    leaf = _leaves(exc)[0]
    text = str(leaf).strip() or type(leaf).__name__
    status = getattr(getattr(leaf, "response", None), "status_code", None)
    if status:
        text = f"HTTP {status}: {text}"
    return _cap(f"{type(leaf).__name__}: {text}", 400)[0]


def _result_text(result: Any) -> str:
    parts = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text is None:
            # An embedded resource (GitHub's get_file_contents returns the file this way).
            text = getattr(getattr(block, "resource", None), "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(f"[{getattr(block, 'type', 'non-text')} content omitted]")
    if not parts and getattr(result, "structured_content", None) is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    return "\n".join(parts)


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n... [truncated {len(text) - limit} characters]", True
