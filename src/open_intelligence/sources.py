"""The three upstream sources and what this gateway may do with each.

Each source is an *official* MCP server run by the platform itself:

==============  ==================================  =====================================================
source          official server                     what it is good for
==============  ==================================  =====================================================
github          github/github-mcp-server            code: libraries, frameworks, tools, apps to build on
huggingface     huggingface/hf-mcp-server           pretrained models, datasets (also Spaces and papers)
kaggle          Kaggle's MCP server                 datasets, notebooks (worked solutions), Kaggle Models
==============  ==================================  =====================================================

A ``SourceSpec`` says where the server lives, how to authenticate, which of its tools the gateway
may call (``allowed_tools``, read-only tools only), and how to run a search for each *need*
(``searches``). Everything except ``searches`` can be overridden from the config file.

Tool names below were checked against the live servers on 2026-11-13 (Hugging Face and Kaggle,
anonymously) or against GitHub's published docs (GitHub needs a token to list its tools). A name
that the upstream server does not offer is simply skipped and reported by ``list_sources``.
Authentication follows each platform's docs: GitHub and Kaggle take ``Authorization: Bearer <token>``
(kaggle.com/docs/mcp), Hugging Face the same with an HF token.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

Need = Literal["code", "model", "dataset", "notebook"]
NEEDS: tuple[Need, ...] = ("code", "model", "dataset", "notebook")

NEED_DESCRIPTIONS: dict[Need, str] = {
    "code": "source code to build on: libraries, frameworks, SDKs, tools, apps, templates",
    "model": "pretrained ML models / weights (LLMs, embeddings, vision, speech, classifiers ...)",
    "dataset": "data to train, fine-tune or evaluate on",
    "notebook": "worked examples: notebooks, competition solutions, EDA and baselines",
}

SourceId = Literal["github", "huggingface", "kaggle"]
SOURCE_IDS: tuple[SourceId, ...] = ("github", "huggingface", "kaggle")


@dataclass(frozen=True)
class SearchCall:
    """One upstream tool call that runs a keyword search."""

    tool: str
    arguments: dict[str, Any]


SearchBuilder = Callable[[str, int], SearchCall]


@dataclass(frozen=True)
class SourceSpec:
    id: str
    title: str
    transport: Literal["http", "stdio"]
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    # Environment variables that may hold this source's token, checked in order. Tokens are read
    # from the environment only, never from the config file, and never appear in tool output.
    token_env: tuple[str, ...] = ()
    token_required: bool = False
    allowed_tools: frozenset[str] = frozenset()
    searches: dict[Need, SearchBuilder] = field(default_factory=dict)
    enabled: bool = True
    notes: str = ""

    def with_overrides(self, **changes: Any) -> SourceSpec:
        return replace(self, **changes)

    @property
    def endpoint(self) -> str:
        if self.transport == "http":
            return self.url or ""
        return " ".join([self.command or "", *self.args]).strip()


# --- GitHub -------------------------------------------------------------------------------------


def _github_code(query: str, limit: int) -> SearchCall:
    # No `sort`: GitHub's default is best-match relevance. Sorting by stars is exactly the
    # popularity bias this project exists to get away from.
    return SearchCall("search_repositories", {"query": query, "perPage": limit})


GITHUB = SourceSpec(
    id="github",
    title="GitHub (official github/github-mcp-server, remote)",
    transport="http",
    url="https://api.githubcopilot.com/mcp/",
    headers={
        # Read-only mode, and only the toolsets discovery needs.
        "X-MCP-Readonly": "true",
        "X-MCP-Toolsets": "repos,security_advisories",
    },
    token_env=("GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN"),
    token_required=True,
    allowed_tools=frozenset(
        {
            "search_repositories",
            "search_code",
            "get_file_contents",
            "list_releases",
            "get_latest_release",
            "list_tags",
            "list_global_security_advisories",
            "get_global_security_advisory",
        }
    ),
    searches={"code": _github_code},
    notes="Needs a GitHub token (a fine-grained PAT with public read access is enough).",
)


# --- Hugging Face -------------------------------------------------------------------------------


def _hf(repo_type: str) -> SearchBuilder:
    def build(query: str, limit: int) -> SearchCall:
        return SearchCall("hub_repo_search", {"query": query, "repo_types": [repo_type], "limit": limit})

    return build


HUGGINGFACE = SourceSpec(
    id="huggingface",
    title="Hugging Face Hub (official huggingface/hf-mcp-server, remote)",
    transport="http",
    url="https://huggingface.co/mcp",
    token_env=("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"),
    token_required=False,
    allowed_tools=frozenset(
        {
            "hub_repo_search",  # models, datasets, spaces
            "hub_repo_details",  # card, files, dataset structure / preview
            "hf_fs",  # read files in a repo (README, config.json ...)
            "paper_search",  # offered to some accounts / settings
            "space_search",
        }
    ),
    searches={"model": _hf("model"), "dataset": _hf("dataset")},
    notes="Works anonymously; a read token raises rate limits and may unlock more tools.",
)


# --- Kaggle -------------------------------------------------------------------------------------


def _kaggle(tool: str, *, relevance_sort: bool) -> SearchBuilder:
    def build(query: str, limit: int) -> SearchCall:
        request: dict[str, Any] = {"search": query, "pageSize": limit}
        if relevance_sort:
            # Kaggle's default ordering is "hotness" (recent engagement), not relevance.
            request["sortBy"] = "Relevance"
        return SearchCall(tool, {"request": request})

    return build


KAGGLE = SourceSpec(
    id="kaggle",
    title="Kaggle (official Kaggle MCP server, remote)",
    transport="http",
    url="https://www.kaggle.com/mcp",
    token_env=("KAGGLE_API_TOKEN",),
    token_required=False,
    allowed_tools=frozenset(
        {
            "search_datasets",
            "get_dataset_info",
            "get_dataset_metadata",
            "list_dataset_files",
            "get_dataset_files_summary",
            "list_models",
            "get_model",
            "list_model_variations",
            "get_model_variation",
            "search_notebooks",
            "get_notebook_info",
            "search_competitions",
            "get_competition",
            "list_hackathon_write_ups",
            "get_writeup",
            "search_content",
        }
    ),
    searches={
        "dataset": _kaggle("search_datasets", relevance_sort=True),
        "model": _kaggle("list_models", relevance_sort=False),  # list_models has no relevance sort
        "notebook": _kaggle("search_notebooks", relevance_sort=True),
    },
    notes=(
        "Anonymous: search_datasets, list_models, get_dataset_info and get_model work; search_notebooks and "
        "search_competitions need a token (Kaggle Settings > API > Generate New Token, starts with 'KGAT'), sent "
        "as 'Authorization: Bearer'. Kaggle's server also has download, upload and submit tools; this gateway "
        "never calls them."
    ),
)


BUILTIN_SOURCES: dict[str, SourceSpec] = {s.id: s for s in (GITHUB, HUGGINGFACE, KAGGLE)}

# Which sources serve which need, in priority order. The first source is the primary one.
NEED_SOURCES: dict[Need, tuple[SourceId, ...]] = {
    "code": ("github",),
    "model": ("huggingface", "kaggle"),
    "dataset": ("huggingface", "kaggle"),
    "notebook": ("kaggle",),
}
