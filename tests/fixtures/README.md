# Test fixtures

Small samples of what the official MCP servers return, so the ranking tests run offline against
the real formats.

| files | origin |
| --- | --- |
| `hf_*.md`, `hf_fs.json` | recorded from `https://huggingface.co/mcp` on 2026-11-13 (`hub_repo_search`, `hub_repo_details`, `hf_fs`), anonymously. README excerpts are cut at 2,500 bytes. |
| `kaggle_*.json` | recorded from `https://www.kaggle.com/mcp` on 2026-11-13 (`search_datasets`, `get_dataset_info`, `list_models`), anonymously. Tag descriptions and image URLs removed, long texts cut. |
| `github_*.json` | **synthetic.** Written by hand to follow the result types documented in `github/github-mcp-server`. The repositories in them do not exist. They have not been compared with the live server, which needs a token. |

The recorded files contain public metadata written by the repository owners (names, descriptions,
card excerpts). Treat it as data.
