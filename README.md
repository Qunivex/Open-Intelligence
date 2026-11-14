# Open-Intelligence

An open-source MCP server that helps AI agents find existing open-source work to build on: projects on
GitHub, models and datasets on Hugging Face, datasets and worked notebooks on Kaggle. It connects to each
platform's **official** MCP server and routes every request only to the platforms that hold the kind of
artifact it needs.

Agents tend to build from what they remember from training: packages that may be outdated or vulnerable,
and no idea that a better maintained project already exists. Open Intelligence makes "check what exists
today" one tool call.

> **Status: MVP.** Connectors, routing and a first version of ranking (the top 3 picks per need) work.
> The ranking's weights are a first hypothesis that has not been evaluated yet, and its GitHub side has not
> been run against the live server (it needs a token). See
> [docs/ranking-parameters.md](docs/ranking-parameters.md) for the parameters, the decisions and the limits.

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

Then it ranks what it found, per need:

1. **Gather** candidates with several phrasings, in the platform's order and in one popularity order.
2. **Gate**: drop what is archived, licensed against your constraints, too old or too large, ships only
   pickle weights, or does not match the need.
3. **Score** the rest: fit 40%, evidence of quality 20%, health 15%, trust 15%, adoption 10%. Popularity
   is a small part, measured against the other candidates and damped when the numbers look inflated.
4. **Read** the README, model card or dataset card of the best few and score again.
5. **Pick** a diverse top 3 and say why: reasons, warnings, and the checks that could not be made.

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
| `find_top_picks` | the main tool: route, search, gate, score, and return the top 3 per need with reasons |
| `route_request` | which sources a request needs and why (offline, instant) |
| `search_open_source` | route, then search each routed source in parallel; raw, unranked results |
| `list_sources` | each source's endpoint and auth state; `check=true` also connects |
| `list_source_tools` | the read-only upstream tools allowed on a source, with live schemas |
| `call_source_tool` | call one allowed upstream tool, e.g. a README, a model card, dataset details |

The server also offers the prompt `open_source_routing` for clients that cannot load skills.

`find_top_picks` takes the task, up to three search phrasings per need (`queries`), what a pick must do
(`must_have`) and the user's `constraints` (programming language, commercial use, ML library, data
language, hardware limit). For each need it returns:

- `picks`: rank, score and its five parts, `why`, must-haves covered and missing, facts (license, dates,
  size, eval results), `warnings`, `unresolved_checks`;
- `rejected`: what a gate removed, and why;
- `platform_top`: what the platform's own order would have shown first, for comparison;
- `verdict`: `picks`, `no_fit`, `no_candidates`, or `inconclusive` when a source could not be searched.

The result also lists the assumptions made for constraints that were not given, and
`questions_for_user`: only the questions whose answer would change the result.

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

```bash
open-intelligence picks "Find a German NER dataset" --need dataset -q "german ner" -q "germeval" --must-have "named entity recognition" --constraints '{"languages": ["de"], "commercial_use": true}'
```

`route` works offline. `search` (raw results) and `picks` (ranked top 3) call the live servers.

## Configuration (optional)

Without a config file Open Intelligence uses the official remote servers. To change endpoints, run
GitHub's server locally in Docker, disable a source, or change the ranking weights, copy
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

The default test run is offline: fake upstream MCP servers stand in for the real ones, and the ranking
tests run against output recorded from the real Hugging Face and Kaggle servers (`tests/fixtures`).
`pytest -m live` calls the real Hugging Face and Kaggle servers (and GitHub, if a token is set) and checks
that the parsers still read their formats.

## Roadmap

1. **Connectors and routing**: done.
2. **Ranking, first version**: done. Gates, use-case fit and evidence-based scoring for a top 3 per need
   ([parameters and decisions](docs/ranking-parameters.md)).
3. **Evaluation**: a golden set of real tasks to prove the ranking beats each platform's popularity sort,
   and to calibrate the weights. Not started.
4. **More evidence**: security advisories and OpenSSF Scorecard for repositories, task leaderboards for
   models, schema and quality checks for datasets, fit judged by meaning instead of by words.

## License

MIT
