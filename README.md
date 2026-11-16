# Open-Intelligence

Open-Intelligence is an MCP server for finding existing open-source projects, models, datasets and
notebooks to build on. It connects to the official MCP servers for GitHub, Hugging Face and Kaggle, then
routes each request to the sources relevant to the type of resource needed.

> **Status: MVP.** The server supports source connectors, routing, ranking and an evaluation harness. In
> the current evaluation seed set, the ranking's mean nDCG@3 is 0.66, compared with 0.56 for the best
> platform order. It wins on 3 of 10 tasks, ties on 3 and loses on 4. These early results do not establish
> that the ranking is better. All three platforms have been tested live. See
> [docs/evaluation.md](docs/evaluation.md) for the results and
> [docs/ranking-parameters.md](docs/ranking-parameters.md) for the ranking design and its limitations.

## How it works

```
agent ──MCP──> Open Intelligence ──MCP──> api.githubcopilot.com/mcp   (GitHub, read-only)
                │  router                ├─> huggingface.co/mcp         (Hugging Face)
                │  connector             └─> www.kaggle.com/mcp         (Kaggle)
```

The router identifies the types of resources a request needs and selects the corresponding platforms:

| need | meaning | searched on |
| --- | --- | --- |
| `code` | libraries, frameworks, SDKs, tools, apps | GitHub |
| `model` | pretrained ML models | Hugging Face, then Kaggle Models |
| `dataset` | data to train, fine-tune or evaluate on | Hugging Face, Kaggle |
| `notebook` | worked examples, competition solutions | Kaggle |

"Build a Python project that automates a Snowflake pipeline from Census" needs code, so Open-Intelligence
searches GitHub. "Fine-tune a sentiment classifier on our reviews" needs code, a base model and a dataset,
so it searches all three platforms. See [docs/architecture.md](docs/architecture.md) for details.

Results are ranked separately for each resource type:

1. **Gather** candidates using several query phrasings and both relevance and popularity-based orders.
2. **Filter** candidates that fail relevant checks, such as incompatible licenses, known critical
   vulnerabilities, age or size limits, unsafe weight formats, or poor fit.
3. **Score** the remaining candidates: fit 40%, quality evidence 20%, health 15%, trust 15% and adoption
   10%. Adoption is measured relative to other candidates and reduced when its signals appear inflated.
4. **Review** the strongest candidates and refine their scores:
   - repositories: README, latest release, OpenSSF Scorecard, known vulnerabilities in the current release;
   - models: model card, evaluation results on shared benchmarks and weight files;
   - datasets: dataset card, splits, row counts, columns and a sample of rows.
5. **Select** up to three diverse candidates per need and report the reasons, warnings and unchecked items.

Repository security evidence comes from two public services that are not MCP servers:
[OpenSSF Scorecard](https://scorecard.dev) and [deps.dev](https://deps.dev). Only the candidate's
repository and package names are sent to these services. Both checks can be disabled in the config file.

## Install

Requires Python 3.11 or later.

```bash
git clone https://github.com/Qunivex/Open-Intelligence.git
cd Open-Intelligence && pip install -e .
```

## Tokens

The server reads tokens from environment variables or a supported `.env` file. Do not put token values in
the TOML configuration or in tool calls.

| source | variable | needed? |
| --- | --- | --- |
| GitHub | `GITHUB_PERSONAL_ACCESS_TOKEN` (or `GITHUB_TOKEN`) | Required. A fine-grained token with read access to public repositories is sufficient. |
| Hugging Face | `HF_TOKEN` | Optional. Search works without a token; a read token can increase rate limits. |
| Kaggle | `KAGGLE_API_TOKEN` | Required for notebook and competition searches. Dataset and model searches work without a token. Create one under Kaggle Settings > API. |

Create a `.env` file using `NAME=value` lines in the directory where you run the server, or in
`~/.config/open-intelligence/`:

```
GITHUB_PERSONAL_ACCESS_TOKEN=github_pat_...
```

`.env` is listed in `.gitignore`. Do not put tokens in `open-intelligence.toml` or the example file.
The `token_env` setting takes an environment-variable name, not a token value.

Check source connectivity with:

```bash
open-intelligence sources --check
```

## Use it from an MCP client

For Claude Code, add this configuration to the project's `.mcp.json`:

```json
{
  "mcpServers": {
    "open-intelligence": {
      "command": "open-intelligence",
      "args": ["serve"]
    }
  }
}
```

The server reads credentials from its environment or supported `.env` file; Claude Code does not load
project `.env` values through `${...}` references. Any MCP client that can launch a stdio server can use
the command `open-intelligence serve`.

### Tools

| Tool | Description |
| --- | --- |
| `find_top_picks` | Routes and searches each need, filters and ranks candidates, and returns up to three results with supporting reasons. |
| `route_request` | Reports which sources a request needs and why. Works offline. |
| `search_open_source` | Searches routed sources in parallel and returns unranked results. |
| `list_sources` | Lists source endpoints and authentication status. Set `check=true` to test connectivity. |
| `list_source_tools` | Lists the read-only upstream tools available for a source, including their live schemas. |
| `call_source_tool` | Calls an allowed upstream tool, such as one that retrieves a README, model card or dataset details. |

The server also provides the `open_source_routing` prompt for clients that cannot load skills.

`find_top_picks` accepts a task description, up to three search phrasings per need (`queries`), required
capabilities (`must_have`) and user constraints such as programming language, commercial use, ML library,
data language or hardware limits. For each need, it returns:

- `picks`: rank, score and its five components; reasons; which required capabilities are covered or
  missing; available facts (license, dates, size, evaluation results, dataset structure and security
  evidence); warnings; and unchecked items.
- `benchmark_comparison` for models, where two or more candidates report the same benchmark.
- `rejected`, with candidates removed by a filter and the reason for each removal.
- `platform_top`, showing what the platform's own order would have returned first.
- `verdict`: `picks`, `no_fit`, `no_candidates` or `inconclusive` if a source could not be searched.

The result also lists assumptions made about unspecified constraints and includes `questions_for_user`
only when an answer could change the result.

### The routing skill

The [`open-source-routing` skill](src/open_intelligence/skills/open-source-routing/SKILL.md) guides an
agent in identifying needs, writing platform-specific queries and reviewing candidates. Install it for
Claude Code with:

```bash
cp -r "$(open-intelligence skill-path)" ~/.claude/skills/
```

## Command line

```bash
open-intelligence route "Python project to send data from Census to Snowflake"
```

```bash
open-intelligence search "Fine-tune a sentiment classifier on product reviews" -q "sentiment" --limit 3
```

```bash
open-intelligence picks "Find a German NER dataset" --need dataset -q "german ner" -q "germeval" --must-have "named entity recognition" --constraints '{"languages": ["de"], "commercial_use": true}'
```

```bash
open-intelligence eval
```

`route` works offline. `search` returns raw results and `picks` returns ranked results; both call the live
servers. `eval` compares the ranking with a golden set and each platform's own order
([docs/evaluation.md](docs/evaluation.md)).

## Configuration (optional)

Without a config file, Open-Intelligence uses the official remote servers. To change endpoints, run
GitHub's server locally in Docker, disable a source or security check, or adjust ranking weights, copy
[`open-intelligence.example.toml`](open-intelligence.example.toml) to `./open-intelligence.toml` or
`~/.config/open-intelligence/config.toml`.

## Safety

- Only read-only discovery tools are available. Upload, download and submission tools on Kaggle and
  write tools on GitHub are blocked; GitHub is also connected in read-only mode.
- Tokens are sent only to their respective platforms and are not included in results, errors or status.
- Security lookups send candidate repository and package names to `api.scorecard.dev` and `api.deps.dev`.
  They do not receive details about your request or project. Disable them with `scorecard = false` and
  `advisories = false` under `[evidence]`.
- Third-party text in results is marked as `untrusted_content`.

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

The default test run is offline. It uses mock upstream MCP servers and fixtures recorded from the real
Hugging Face and Kaggle servers (`tests/fixtures`). `pytest -m live` calls the live Hugging Face and Kaggle
servers, and GitHub when a token is set, to check that the parsers handle their responses.


## License

MIT
