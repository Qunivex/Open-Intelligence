from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import Client

from open_intelligence.config import Settings
from open_intelligence.connector import Connector, SourceError
from open_intelligence.sources import BUILTIN_SOURCES

FAKE = Path(__file__).with_name("fake_upstream.py")
FAKE_TOKENS = {"github": "FAKE_GH_TOKEN", "huggingface": "FAKE_HF_TOKEN", "kaggle": "FAKE_KAGGLE_TOKEN"}
SECRET = "ghp_test_secret_value_123"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def fake_settings(**gateway: float | int) -> Settings:
    """The real source specs (allowlists, search builders), each pointed at a local fake server."""
    sources = {}
    for sid, spec in BUILTIN_SOURCES.items():
        sources[sid] = spec.with_overrides(
            transport="stdio",
            command=sys.executable,
            args=(str(FAKE), sid),
            url=None,
            headers={},
            token_env=(FAKE_TOKENS[sid],),
        )
    # Let the tests reach the fake's oversized tool, to check the output cap.
    sources["kaggle"] = sources["kaggle"].with_overrides(
        allowed_tools=sources["kaggle"].allowed_tools | {"big_output"}
    )
    return Settings(
        sources=sources,
        timeout_s=float(gateway.get("timeout_s", 30)),
        max_result_chars=int(gateway.get("max_result_chars", 12_000)),
    )


@pytest.fixture
def connector() -> Connector:
    # GitHub requires a token (as the real one does); the fake GitHub sees it in its environment.
    return Connector(fake_settings(), environ={"FAKE_GH_TOKEN": SECRET})


RANKED_FAKE = Path(__file__).with_name("fake_ranked_upstream.py")


def ranked_settings() -> Settings:
    """The real source specs, each pointed at a fake server that answers with recorded fixtures."""
    sources = {
        sid: spec.with_overrides(
            transport="stdio",
            command=sys.executable,
            args=(str(RANKED_FAKE), sid),
            url=None,
            headers={},
            token_env=(FAKE_TOKENS[sid],),
        )
        for sid, spec in BUILTIN_SOURCES.items()
    }
    return Settings(sources=sources, timeout_s=30)


class InProcessConnector(Connector):
    """Reaches the fixture servers inside the test process instead of starting three subprocesses.

    Everything above the transport is the real code: allowlist, token checks, timeouts, error
    handling. The stdio transport itself is covered by test_connector.py and one test in test_server.py.
    """

    @asynccontextmanager
    async def open(self, source_id: str) -> AsyncIterator[Client]:
        from tests.fake_ranked_upstream import make

        reason = self.unavailable_reason(self.spec(source_id))
        if reason:
            raise SourceError(reason)
        async with Client(make(source_id)) as client:
            yield client


def ranked(environ: dict[str, str] | None = None) -> Connector:
    return InProcessConnector(
        ranked_settings(), environ={"FAKE_GH_TOKEN": SECRET} if environ is None else environ
    )


@pytest.fixture
def ranked_connector() -> Connector:
    return ranked()


@pytest.fixture
def ranked_stdio_connector() -> Connector:
    return Connector(ranked_settings(), environ={"FAKE_GH_TOKEN": SECRET})
