# Open-Intelligence

An open-source MCP server that helps AI agents find existing open-source work to build on: projects on
GitHub, models and datasets on Hugging Face, datasets and worked notebooks on Kaggle. It connects to each
platform's **official** MCP server and routes every request only to the platforms that hold the kind of
artifact it needs.

Agents tend to build from what they remember from training: packages that may be outdated or vulnerable,
and no idea that a better maintained project already exists. Open Intelligence makes "check what exists
today" one tool call.

> **Status: MVP.** Connectors, routing, ranking (the top 3 picks per need) and an evaluation harness work.
> The evaluation so far, on a small seed set, puts the ranking somewhat ahead of the platforms' own order
> (nDCG@3 0.62 against 0.56) but behind it on 5 of 10 tasks: it is not yet shown to be better. All three
> platforms have been run live. See
> [docs/evaluation.md](docs/evaluation.md) for the results and
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
2. **Gate**: drop what is archived, has a critical known vulnerability, is licensed against your
   constraints, too old or too large, ships only pickle weights, or does not match the need.
3. **Score** the rest: fit 40%, evidence of quality 20%, health 15%, trust 15%, adoption 10%. Popularity
   is a small part, measured against the other candidates and damped when the numbers look inflated.
4. **Read** the best few and score again:
   - repositories: README, latest release, OpenSSF Scorecard, known vulnerabilities in the current release;
   - models: the card, its eval results (compared with other candidates on the same benchmark), weight files;
   - datasets: the card, splits and row counts, column names, and a sample of rows.
5. **Pick** a diverse top 3 and say why: reasons, warnings, and the checks that could not be made.

The security evidence for repositories comes from two public services that are not MCP servers:
[OpenSSF Scorecard](https://scorecard.dev) and [deps.dev](https://deps.dev). Only a candidate's
repository and package name are sent to them, and both can be switched off in the config file.

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/Qunivex/Open-Intelligence.git
```

```bash
cd Open-Intelligence && pip install -e .
```

## Tokens

Tokens are read from environment variables, or from a `.env` file, and never from the TOML config or
from tool calls.

| source | variable | needed? |
| --- | --- | --- |
| GitHub | `GITHUB_PERSONAL_ACCESS_TOKEN` (or `GITHUB_TOKEN`) | **yes**: GitHub's MCP server needs a token. A fine-grained PAT with read access to public repositories is enough. |
| Hugging Face | `HF_TOKEN` | no: search works anonymously; a read token raises rate limits |
| Kaggle | `KAGGLE_API_TOKEN` | for notebook and competition search; dataset and model search work anonymously. Create one under Kaggle Settings > API (it starts with `KGAT`). |

The simplest setup is a `.env` file (`NAME=value` lines) in the directory you run from, or in
`~/.config/open-intelligence/`:

```
GITHUB_PERSONAL_ACCESS_TOKEN=github_pat_...
```

`.env` is in `.gitignore`. Do not put a token in `open-intelligence.toml` or the example file: `token_env`
there takes the *name* of the variable, and a token pasted in its place is refused.

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
  size, eval results, dataset splits and columns, Scorecard and known vulnerabilities), `warnings`,
  `unresolved_checks`;
- `benchmark_comparison` (models): the numbers side by side, wherever candidates report the same benchmark;
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

```bash
open-intelligence eval
```

`route` works offline. `search` (raw results) and `picks` (ranked top 3) call the live servers. `eval`
scores the ranking against a golden set and against each platform's own order
([docs/evaluation.md](docs/evaluation.md)).

## Configuration (optional)

Without a config file Open Intelligence uses the official remote servers. To change endpoints, run
GitHub's server locally in Docker, disable a source, switch off the security lookups, or change the
ranking weights, copy
[`open-intelligence.example.toml`](open-intelligence.example.toml) to `./open-intelligence.toml` or
`~/.config/open-intelligence/config.toml`.

## Safety

- Only read-only discovery tools are reachable. Kaggle's upload, download and submit tools, and GitHub's
  write tools, are refused. GitHub is also connected in read-only mode.
- Tokens are sent only to their own platform and never appear in results, errors or status.
- The security lookups send the repository and package names of candidates to `api.scorecard.dev` and
  `api.deps.dev`, and nothing about your request or project. Set `scorecard = false` and
  `advisories = false` under `[evidence]` to switch them off.
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
2. **Ranking**: done. Gates, use-case fit and evidence-based scoring for a top 3 per need, with OpenSSF
   Scorecard and known vulnerabilities for repositories, benchmark comparison for models and structure
   checks for datasets ([parameters and decisions](docs/ranking-parameters.md)).
3. **Evaluation harness**: done. Golden set format, nDCG@3 and precision@3 against each platform's own
   order, ablations, record and replay ([first results](docs/evaluation.md)).
4. **A real golden set**: 30 to 50 tasks per type, graded by practitioners. The one that ships is a seed
   written by the assistant that built the ranking. Not started.
5. **What the evaluation says to fix**: re-uploads under other names still outranking the original
   dataset, the pickle-only gate removing standard models, card-declared evals carrying weight they do not
   earn, adoption deserving more weight than it has, fit judged by words instead of meaning.

## License

MIT
