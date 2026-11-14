"""A stand-in upstream MCP server that answers with recorded fixtures, for the ranking tests.

    python fake_ranked_upstream.py github|huggingface|kaggle

It offers the tools the ranking pipeline calls and returns the files in tests/fixtures: real
recordings for Hugging Face and Kaggle, hand-written ones for GitHub (see fixtures/README.md).
The answer does not depend on the query: every search returns the same recorded list.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

FIXTURES = Path(__file__).with_name("fixtures")


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _data(name: str) -> Any:
    return json.loads(_text(name))


def make(source: str) -> MCPServer:
    server = MCPServer(name=f"fake-ranked-{source}")

    if source == "github":
        files = _data("github_files.json")

        @server.tool()
        def search_repositories(
            query: str, perPage: int = 30, minimal_output: bool = True, sort: str = "", order: str = ""
        ) -> str:
            return _text("github_search_repositories.json")

        @server.tool()
        def get_file_contents(owner: str, repo: str, path: str = "/") -> str:
            entry = files.get(f"{owner}/{repo}")
            if not entry or path != "README.md":
                raise ToolError("failed to get file contents: 404 Not Found")
            return "successfully downloaded text file (SHA: abc123)\n" + entry["readme"]

        @server.tool()
        def get_latest_release(owner: str, repo: str) -> str:
            entry = files.get(f"{owner}/{repo}")
            if not entry or not entry.get("release"):
                raise ToolError("failed to get latest release: 404 Not Found")
            return json.dumps(entry["release"])

    elif source == "huggingface":
        recorded = _data("hf_fs.json")

        @server.tool()
        def hub_repo_search(
            query: str = "", repo_types: list[str] | None = None, limit: int = 20, sort: str = ""
        ) -> str:
            kind = (repo_types or ["model"])[0]
            return _text(f"hf_search_{kind}{'_downloads' if sort == 'downloads' else ''}.md")

        @server.tool()
        def hub_repo_details(repo_ids: list[str], repo_type: str = "model") -> str:
            return _text(f"hf_details_{repo_type}.md")

        @server.tool()
        def hf_fs(operations: list[dict[str, Any]]) -> dict[str, Any]:
            results = []
            for index, op in enumerate(operations):
                entry = recorded.get(f"{op['cmd']} {op['args'][0]}")
                if entry is None:
                    entry = {
                        "status": "error",
                        "error": {"code": "HF_FS_NOT_FOUND", "message": "not recorded"},
                    }
                results.append({"index": index, **entry})
            return {"results": results}

    else:
        info = _data("kaggle_dataset_info.json")

        @server.tool()
        def search_datasets(request: dict[str, Any]) -> str:
            return _text("kaggle_search_datasets.json")

        @server.tool()
        def get_dataset_info(request: dict[str, Any]) -> str:
            ref = f"{request.get('ownerSlug')}/{request.get('datasetSlug')}"
            if ref not in info:
                raise ToolError("Not found")
            return json.dumps(info[ref])

        @server.tool()
        def list_models(request: dict[str, Any]) -> str:
            return _text("kaggle_list_models.json")

        @server.tool()
        def search_notebooks(request: dict[str, Any]) -> str:
            raise ToolError("Unauthenticated")

    return server


if __name__ == "__main__":
    make(sys.argv[1]).run("stdio")
