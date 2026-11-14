# Open-Intelligence

An open-source MCP server that helps AI agents find existing open-source work to build on: projects on
GitHub, models and datasets on Hugging Face, datasets and worked notebooks on Kaggle. It connects to each
platform's **official** MCP server and routes every request only to the platforms that hold the kind of
artifact it needs.

Agents tend to build from what they remember from training: packages that may be outdated or vulnerable,
and no idea that a better maintained project already exists. Open Intelligence makes "check what exists
today" one tool call.

> **Status: MVP.** Connectors and routing work. Ranking (the top 3 picks) is not built yet; it is being
> researched in [docs/ranking-parameters.md](docs/ranking-parameters.md). Results today keep each
> platform's own order.

## How it works

```
agent ──MCP──> Open Intelligence ──MCP──> api.githubcopilot.com/mcp   (GitHub, read-only)
                │  router                ├─> huggingface.co/mcp         (Hugging Face)
                │  connector             └─> www.kaggle.com/mcp         (Kaggle)
```

The router decides what the request needs, then which platforms serve that need:

| need | meaning | searched on |
| --- | --- | --- |
| `code` | libraries, frameworks, SDKs, tools, apps | GitHub |
| `model` | pretrained ML models | Hugging Face, then Kaggle Models |
| `dataset` | data to train, fine-tune or evaluate on | Hugging Face, Kaggle |
| `notebook` | worked examples, competition solutions | Kaggle |

"Build a Python project that automates a Snowflake pipeline from Census" needs code only, so only GitHub is
searched. "Fine-tune a sentiment classifier on our reviews" needs code, a base model and a dataset, so all
three are. See [docs/architecture.md](docs/architecture.md) for the design.

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/Qunivex/Open-Intelligence.git
```

```bash
cd Open-Intelligence && pip install -e .
```

## Tokens

Tokens are read from environment variables only, never from files or tool calls.

| source | variable | needed? |
| --- | --- | --- |
| GitHub | `GITHUB_PERSONAL_ACCESS_TOKEN` (or `GITHUB_TOKEN`) | **yes**: GitHub's MCP server needs a token. A fine-grained PAT with read access to public repositories is enough. |
| Hugging Face | `HF_TOKEN` | no: search works anonymously; a read token raises rate limits |
| Kaggle | `KAGGLE_API_TOKEN` | for notebook and competition search; dataset and model search work anonymously. Create one under Kaggle Settings > API (it starts with `KGAT`). |

Check what is connected:

```bash
open-intelligence sources --check
```

## Use it from an MCP client

Claude Code, in a project's `.mcp.json` (the `${...}` values are read from your environment):

```json
{
  "mcpServers": {
    "open-intelligence": {
      "command": "open-intelligence",
      "args": ["serve"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}",
        "HF_TOKEN": "${HF_TOKEN}",
        "KAGGLE_API_TOKEN": "${KAGGLE_API_TOKEN}"
      }
    }
  }
}
```

Any MCP client that can launch a stdio server works the same way: the command is `open-intelligence serve`.

### Tools

| tool | what it does |
| --- | --- |
| `route_request` | which sources a request needs and why (offline, instant) |
| `search_open_source` | route, then search each routed source in parallel; raw, unranked results |
| `list_sources` | each source's endpoint and auth state; `check=true` also connects |
| `list_source_tools` | the read-only upstream tools allowed on a source, with live schemas |
| `call_source_tool` | call one allowed upstream tool, e.g. a README, a model card, dataset details |

The server also offers the prompt `open_source_routing` for clients that cannot load skills.

### The routing skill

[`open-source-routing`](src/open_intelligence/skills/open-source-routing/SKILL.md) teaches an agent how to
decide the needs, phrase queries for each platform, and inspect candidates before recommending one.
To install it for Claude Code:

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

`route` works offline. `search` calls the live servers.

## Configuration (optional)

Without a config file Open Intelligence uses the official remote servers. To change endpoints, run
GitHub's server locally in Docker, or disable a source, copy
[`open-intelligence.example.toml`](open-intelligence.example.toml) to `./open-intelligence.toml` or
`~/.config/open-intelligence/config.toml`.

## Safety

- Only read-only discovery tools are reachable. Kaggle's upload, download and submit tools, and GitHub's
  write tools, are refused. GitHub is also connected in read-only mode.
- Tokens are sent only to their own platform and never appear in results, errors or status.
- Text in results is written by third parties and is marked `untrusted_content`.

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

The default test run is offline: fake upstream MCP servers stand in for the real ones. `pytest -m live`
calls the real Hugging Face and Kaggle servers (and GitHub, if a token is set).

## Roadmap

1. **Connectors and routing** (this MVP).
2. **Normalized candidates**: one record shape across the three platforms.
3. **Ranking**: gates, use-case fit and evidence-based scoring for a top 3 per need
   ([research](docs/ranking-parameters.md)).
4. **Evaluation**: a golden set of real tasks to prove the ranking beats each platform's popularity sort.

## License

MIT
