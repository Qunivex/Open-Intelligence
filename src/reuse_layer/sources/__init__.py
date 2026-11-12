"""Source adapters: one per host the layer can search. See ``base.py`` for the contract."""

from __future__ import annotations

import httpx

from reuse_layer.config import Settings
from reuse_layer.core.store import Store
from reuse_layer.sources.base import Source
from reuse_layer.sources.github import GitHubClient, GitHubSource


def build_sources(
    settings: Settings, store: Store, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[Source]:
    """Every source this server can use. A new adapter is registered here."""
    github = GitHubClient(
        token=settings.secret("github"),
        api_url=settings.github.api_url,
        budget_ms=settings.github.budget_ms,
        store=store,
        max_concurrency=settings.github.max_concurrency,
        transport=transport,
    )
    return [GitHubSource.from_settings(settings, github)]
