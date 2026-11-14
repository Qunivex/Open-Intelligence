from __future__ import annotations

import sys
from pathlib import Path

import pytest

from open_intelligence.config import Settings
from open_intelligence.connector import Connector
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
