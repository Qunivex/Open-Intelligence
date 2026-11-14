"""A stand-in for one upstream MCP server, run over stdio by the tests.

    python fake_upstream.py github|huggingface|kaggle

It offers the same search tool names as the real server, plus a write tool the gateway must never
call. Every tool echoes its arguments and whether the token env var reached the process (never the
value), so tests can check routing, argument building and token hand-off without the network.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

TOKEN_ENV = {"github": "FAKE_GH_TOKEN", "huggingface": "FAKE_HF_TOKEN", "kaggle": "FAKE_KAGGLE_TOKEN"}


def make(source: str) -> MCPServer:
    server = MCPServer(name=f"fake-{source}")

    def echo(tool: str, args: dict[str, Any]) -> str:
        token_present = bool(os.environ.get(TOKEN_ENV[source]))
        return json.dumps({"source": source, "tool": tool, "args": args, "token_present": token_present})

    if source == "github":

        @server.tool()
        def search_repositories(query: str, perPage: int = 30) -> str:
            return echo("search_repositories", {"query": query, "perPage": perPage})

        @server.tool()
        def get_file_contents(owner: str, repo: str, path: str = "README.md") -> str:
            return echo("get_file_contents", {"owner": owner, "repo": repo, "path": path})

        @server.tool()
        def create_issue(owner: str, repo: str, title: str) -> str:
            return echo("create_issue", {"owner": owner, "repo": repo, "title": title})

    elif source == "huggingface":

        @server.tool()
        def hub_repo_search(query: str = "", repo_types: list[str] | None = None, limit: int = 20) -> str:
            return echo("hub_repo_search", {"query": query, "repo_types": repo_types, "limit": limit})

        @server.tool()
        def hub_repo_details(repo_ids: list[str]) -> str:
            if "explode/please" in repo_ids:
                raise ValueError("repo not found")
            return echo("hub_repo_details", {"repo_ids": repo_ids})

    else:

        @server.tool()
        def search_datasets(request: dict[str, Any]) -> str:
            return echo("search_datasets", request)

        @server.tool()
        def list_models(request: dict[str, Any]) -> str:
            return echo("list_models", request)

        @server.tool()
        def search_notebooks(request: dict[str, Any]) -> str:
            return echo("search_notebooks", request)

        @server.tool()
        def download_dataset(request: dict[str, Any]) -> str:
            return echo("download_dataset", request)

        @server.tool()
        def big_output(request: dict[str, Any]) -> str:
            return "x" * 50_000

    return server


if __name__ == "__main__":
    make(sys.argv[1]).run("stdio")
