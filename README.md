# Open-Intelligence: Reuse Layer v1 (GitHub)

Reuse Layer tells coding agents **what to reuse before they build**. Ask it for a capability and it returns
the **top 3 open-source GitHub projects** that pass every security gate, each pinned to an exact tag and
commit SHA with evidence for the pick, or a clear **"build it yourself"** when nothing fits.

v1 follows the *Reuse Layer: Build Plan* with one deliberate scope cut: **GitHub is the only source.**
Hugging Face, PyPI, npm, deps.dev, OSV and the rest are later versions; the config file already has room
for their keys.

```
user: "I want to build a pricing model that helps people understand the cost of
       implementation using different LLM providers"

agent ── skill: github-oss-discovery ──────────────────────────────────────────────┐
  1. intent brief   job = estimate/compare LLM API cost across providers          │
                    not "option pricing", not an ML "model"                       │
  2. capabilities   per-token price data · token counting · cost calculation      │
  3. hypotheses     direct tools · gateways that embed price tables · tokenizers  │
  4. queries        llm cost estimation · "cost per token" in:readme · …          │
  5. search_github  explore, learn vocabulary from good hits, refine once         │
└─ find_reusable(task, capabilities, candidates, search_queries, avoid, context) ──┘
        │  resolve names → metadata gates → enrich shortlist → capability cards
        │  → relevance → evidence gates → score → top 3 (+1 explore slot) → verdict
        ▼
  picks: pkg:github/owner/repo@v1.2.3  commit 4e71bbe0…  why[…]  gates{…}  risks[…]
```

## Quick start

```bash
pip install -e ".[dev]"              # from this repo (Python 3.11+)
reuse-layer config init              # creates your config file outside any project
```

Open the file it created (`reuse-layer config path` prints where) and paste a GitHub token under `[keys]`.
Create the token at <https://github.com/settings/personal-access-tokens/new> as a **fine-grained token with
"Public repositories (read-only)" and no permissions**. Then:

```bash
reuse-layer doctor                   # checks the config, the token and its scopes (never prints the token)
reuse-layer install-skill            # installs the discovery skill into ~/.claude/skills
claude mcp add reuse-layer --scope user -- reuse-layer mcp
```

Try it from the shell:

```bash
reuse-layer find "Estimate the cost of LLM API usage across providers" -c "LLM pricing data | model prices" -c "token counting | tokenizer" --avoid "option pricing" --language python
```

For detailed local diagnostics, run `reuse-layer -vv find ...` (or set
`REUSE_LAYER_LOG_LEVEL=DEBUG` for an MCP server). Diagnostics go to stderr and include the structured
capabilities/candidates, sanitized GitHub queries, API endpoint/status/cache/timing and response counts. They
never log authorization headers or full API response bodies. Debug queries and repository names can still
contain your search terms, so use debug logging only where those terms are okay to appear in local logs.

## The config file (all API keys live here)

One TOML file, per user, **outside your projects**: `%APPDATA%\reuse-layer\config.toml` on Windows,
`~/Library/Application Support/reuse-layer/config.toml` on macOS, `~/.config/reuse-layer/config.toml` on
Linux (override with `REUSE_LAYER_CONFIG`). The full annotated template is
[src/reuse_layer/config.example.toml](src/reuse_layer/config.example.toml).

```toml
[keys]
github = "github_pat_…"                      # or { env = "VAR" } or { keyring = "reuse-layer" }
anthropic = ""                               # optional: small-model relevance judge
# huggingface = ""                           # later versions
# kaggle = ""
```

Each key can be pasted inline, read from an environment variable (`{ env = "VAR" }`), or read from the OS
keychain (`{ keyring = "reuse-layer" }`, set with `reuse-layer keys set github`). The layer **refuses
inline keys from a config file inside a git repository**, masks every key in logs, and no tool input or
output ever has a key field, so an agent has nothing to leak.

The same file tunes the ranking weights, gates (license policy, minimum age, signed releases), the
relevance judge and storage.

## MCP surface

| Tool | Why it exists | Returns |
| --- | --- | --- |
| `find_reusable` | **The tool.** Top 3 for a task. | Ranked, gated, pinned picks; `rejected` with reasons; `unresolved` invented names; verdict `reuse` / `build_yourself` / `no_candidates` |
| `search_github` | Dependency of the discovery skill: the GitHub token lives in the server, so exploration has to go through it. | Compact results per query (name, about, topics, language, license, last push) |
| `report_outcome` | Dependency of the ranking: the track-record factor (20%) needs integration outcomes. | Acknowledgement and updated retention |

The prompt **`discover_github_projects`** carries the discovery workflow to any MCP client, including
ones that cannot load skills.

## Discovery: intent, not keywords

Splitting a request into keywords finds projects that share its *words*, not its *meaning*. The
[`github-oss-discovery` skill](src/reuse_layer/integrations/skills/github-oss-discovery/SKILL.md) has the
agent:

1. write an **intent brief**: the job to be done, the kind of artifact, constraints, and an ambiguity
   check that turns other meanings of the same words into `avoid` phrases;
2. decompose it into **capabilities**, each with practitioner phrasings (`token counting | tokenizer`);
3. generate **hypotheses** through several lenses: direct solutions, projects that *embed* the capability,
   upstream primitives, standards and formats, adjacent domains, awesome-lists, vendor orgs;
4. build a **query portfolio** of differently angled searches (practitioner phrase, `topic:`,
   `in:readme` phrase, data/artifact, language-scoped, `NOT` the wrong sense);
5. explore with `search_github`, then refine once from the vocabulary of the good hits;
6. hand everything to `find_reusable`, which verifies, gates and ranks it.

The server never trusts the agent's suggestions: every name is resolved against GitHub, and the agent's
opinion adds at most a small agreement bonus to relevance.

## Ranking (design §Ranking algorithm)

```
score(p) = gate(p) · [0.35·relevance + 0.25·trust + 0.20·track record + 0.10·verified + 0.10·tier] + explore
```

| Factor | v1 signal (GitHub REST API) |
| --- | --- |
| Relevance | Capability card (description, topics, README features/headings/intro) scored against the capabilities; optional small-model judge (`claude-haiku-5-5`) blended in when an Anthropic key is set |
| Trust | Contributors and bus factor, push recency, release cadence, age, community health. **Stars are not scored**; they only break exact ties |
| Track record | Retention from `report_outcome` (kept vs replaced/removed, migration votes), Beta-smoothed; neutral 0.5 with no data |
| Verified | Upstream CI (check runs) on the pinned commit; no sandbox in v1 |
| Tier | Owner curation: 1 = foundation/government/public-goods owner (registry), 2 = verified organization, 3 = other |
| Explore | At most one of three slots goes to a **newer** project (or a sudden riser in probation) that passes every gate, chosen by Thompson sampling |

`newer_than_days` measures repository age for exploration; it does not determine whether a release or branch is fresh.
Each pick reports the pinned commit timestamp and default-branch head timestamp in `ref_freshness`, including
whether the branch head is newer and by how many days. The layer still pins to the latest stable release, then
the highest version tag, then the default branch; it does not silently substitute a moving branch for a release.
Commit freshness is reported as evidence and does not currently change the ranking score.

## Security gates (fail closed)

Any failure removes the project; a check that could not complete counts as a failure.

| Gate | Fails when |
| --- | --- |
| exists | the name does not resolve to a real **public** repository (invented names → `unresolved`) |
| status | archived, disabled, a fork, or no push in 730 days |
| license | no license, unrecognised (`NOASSERTION`), or outside the caller's policy (default permissive) |
| age | created less than 30 days ago (unless `allow_new`) |
| fit | primary language incompatible with the caller's project |
| vulns | a published high/critical advisory with no fixed version (repository advisories + GitHub Advisory Database), or the check could not complete |
| malware | a malware advisory for this repository |
| provenance | no commit could be pinned, a signature failed verification, or signatures are required and missing |
| lookalike | the name imitates a more established project and the repo is young or has few contributors |

READMEs are parsed into fixed-schema **capability cards**; text that tries to instruct an AI is detected
and dropped from the card, and the pick is flagged. See [SECURITY.md](SECURITY.md).

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"     # Windows (bin/ on macOS/Linux)
pytest                       # offline: a fake GitHub API, no network
ruff check src tests && ruff format --check src tests && mypy
```

Layout (design §Repo layout, GitHub-only):

```
src/reuse_layer/
  config.py, config.example.toml   the user config file and its template
  core/        engine (pipeline), models, ranking, gates, signals, relevance, cards, discovery, store, text, licenses
  sources/     github.py (REST client + source plugin), registry.toml (tiers, curated owners)
  adapters/    mcp_server.py, cli.py
  integrations/skills/github-oss-discovery/SKILL.md
tests/         offline tests against a fake GitHub API
```

## Not in v1 (from the build plan)

Other sources (PyPI, npm, Hugging Face, government and research catalogs), deps.dev/ecosyste.ms
dependents and one-hop trust, OSV and OpenSSF Scorecard, sandbox verification, `compare`/`verify`/
`check_dependency`, the dependency hook and GitHub Action, the hosted HTTP server with OAuth, and
learning-to-rank. The source, signal and gate code is organised so each can be added as a plugin.
