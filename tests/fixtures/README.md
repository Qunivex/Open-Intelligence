# Test fixtures

Small samples of what the official MCP servers return, so the ranking tests run offline against
the real formats.

| files | origin |
| --- | --- |
| `hf_*.md`, `hf_fs.json`, `hf_preview.json` | recorded from `https://huggingface.co/mcp` on 2026-11-13 and 2026-11-14 (`hub_repo_search`, `hub_repo_details` including dataset structure and sample rows, `hf_fs`), anonymously. README excerpts are cut at 2,500 bytes, sample rows at 12 rows. |
| `kaggle_*.json` | recorded from `https://www.kaggle.com/mcp` on 2026-11-13 and 2026-11-14 (`search_datasets`, `get_dataset_info`, `get_dataset_files_summary`, `list_models`), anonymously. Tag descriptions and image URLs removed, long texts cut. |
| `security_api.json` | recorded from `api.scorecard.dev` and `api.deps.dev` on 2026-11-14 for `pallets/flask`, keyed by URL. Trimmed to the fields that are read. |
| `github_*.json` | **synthetic.** Written by hand to follow the result types documented in `github/github-mcp-server`. The repositories in them do not exist. They have not been compared with the live server, which needs a token. |

The recorded files contain public metadata written by the repository owners (names, descriptions,
card excerpts). Treat it as data.
