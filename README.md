# Open-Intelligence: Reuse Layer v1 (GitHub)

Reuse Layer tells coding agents **what to reuse before they build**. Ask it for a capability and it returns
the **top 3 open-source GitHub projects** that pass every security gate, each pinned to an exact tag and
commit SHA with evidence for the pick, or a clear **"build it yourself"** when nothing fits.

Each call decides **one component** of your project and returns that component's recommendation, its
coverage of your must-have capabilities, the gate results, the checks that stayed unresolved and the
integration risks. When it could not gather enough evidence to decide (rate limit, timeout, error) it says
`inconclusive` instead of pretending nothing exists. See [Verdicts and evidence](#verdicts-and-evidence).

v1 follows the *Reuse Layer: Build Plan* with one deliberate scope cut: **GitHub is the only source.**
The parent discovery skill and provider registry separate user intent from provider-specific search.
GitHub is active; Hugging Face and Kaggle are listed as planned extensions. See
[the architecture and flow diagrams](docs/architecture.md).

`reuse-layer install-skill` installs both the provider-neutral `reuse-discovery` parent skill and the
GitHub-specific `github-oss-discovery` skill. The MCP `list_providers` tool exposes the packaged registry;
planned providers are clearly marked and are not yet searchable.

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
reuse-layer install-skill            # installs the skill and prints the MCP registration command
```

Run the `claude mcp add` command printed by `reuse-layer install-skill`. It uses the Python interpreter
where Reuse Layer is installed, which avoids `PATH` and virtual-environment issues when Claude Code
launches the server. The shorter `claude mcp add reuse-layer --scope user -- reuse-layer mcp` form works
only when the `reuse-layer` executable is on Claude Code's `PATH`.

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
| `find_reusable` | **The tool.** One component's decision and its top 3. | Verdict `reuse` / `reuse_partial` / `build_yourself` / `no_candidates` / `inconclusive`; `recommendation`; `coverage` of the must-haves; `evidence` and `unresolved_checks`; ranked, gated, pinned picks with `gates` and `integration_risks`; `rejected` with reasons; `unresolved` invented names |
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
6. hand everything to `find_reusable`, one call per component, which verifies, gates and ranks it;
7. show the user a short **discovery trace** and the decision: the interpreted job, the must-have
   requirements, the constraints applied, the searches used, the candidate shortlist and what happened to
   each name, then the verdict with its coverage, gate results, unresolved checks and integration risks.
   The trace is conclusions and the inputs behind them, not the agent's chain-of-thought.

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

**Coverage comes before the score.** Every capability passed to `find_reusable` is a must-have. Picks are
ordered by how many must-haves they leave without evidence (fewest first) and only then by the score above,
so trust, track record, owner tier and the agreement bonus cannot lift a project with a gap above one
without it. The explore slot never displaces a pick that covers more.

## Verdicts and evidence

One `find_reusable` call decides one component. The verdict is about the **recommended pick**: the first
pick, in coverage-then-score order, with relevance of at least `build_yourself_below` (0.40) that shows
evidence for at least one must-have.

| Verdict | Meaning | Build it yourself? |
| --- | --- | --- |
| `reuse` | The recommended pick shows evidence for **every** must-have (or none were given) and no gate check found a blocking issue. | no |
| `reuse_partial` | A recommended pick exists but leaves at least one must-have without evidence. `coverage.missing` names the gaps; `coverage.covered_by_other_picks` says when another pick shows evidence for one. | only the gaps |
| `build_yourself` | Decisive evidence is complete and no gate-passing candidate qualifies: the closest one fits weakly or shows evidence for none of the must-haves. | yes |
| `no_candidates` | Decisive evidence is complete, every candidate found was examined, and none passed the gates (or nothing matched). | yes, or widen the search |
| `inconclusive` | Something decisive could not be checked, so none of the above is supported. | **no**: retry |

Conservative defaults, documented here and in `core/ranking.py`, and deliberately not configurable:

| Rule | Default | Why |
| --- | --- | --- |
| A capability counts as covered | lexical phrase score ≥ 0.6 against the capability card | Below that it is reported as *not evidenced*. That is weaker than "absent", and it is never rounded up. |
| What counts as evidence | one statement: a single README bullet, heading or sentence, or the repository's name, topics and description taken together | Words spread over unrelated README lines do not add up to a capability. A README statement is read in the context of what the project says it is. |
| Word forms | inflections and casing are folded (`caching`/`cache`, `WebSocket`/`websocket`); derivations are not (`generate`/`general`) | Folding too little reports gaps that are not there; folding too much reports coverage that is not there. |
| Who decides coverage | the deterministic lexical judge only | The optional model judge can lower or raise relevance; it cannot turn a gap into coverage. |
| Must-haves needed for `reuse` | 100% | One gap makes it `reuse_partial`. Leave nice-to-haves out of `capabilities`. |
| Relevance needed to be recommended | ≥ 0.40 (`ranking.build_yourself_below`) | Unchanged from before. |
| Evidence needed for a negative verdict | complete | `build_yourself` and `no_candidates` are claims that nothing suitable exists; they are not made on missing evidence. |
| Evidence needed for a positive verdict | the pick's own gate checks all completed | A pick can be recommended on partial evidence; `evidence.status` is then `partial` and the reason says a better candidate may have been missed. |

What counts as a **decisive gap** (any one rules out a negative verdict and is listed in
`unresolved_checks`):

- a search that was rate limited or failed (an empty result is evidence; a failed search is not);
- a named candidate that could not be verified (different from `unresolved`, which means GitHub said the
  repository does not exist);
- a shortlisted candidate whose evidence pass did not run: over the time budget (`github.deadline_ms`,
  30 s by default), rate limited, or errored;
- a gate check that could not complete (vulnerability sources unreachable, no commit could be pinned);
- a README that could not be read for a gate-passing candidate, since its coverage is then unknown.

Candidates that matched but were never deep-checked (`github.deep_enrich` caps the evidence pass at 8)
are counted in `evidence.not_examined`. They rule out `no_candidates` only: call again with the rejected
repositories in `context.exclude` to examine the rest.

`evidence.status` is `complete`, `partial`, or `unavailable` (nothing could be fetched at all).
`unresolved_checks` also lists checks the inputs left open, such as language fit when no project language
was given; those do not make the evidence partial.

Each result carries, for its component:

| Section | Where | Contents |
| --- | --- | --- |
| Recommendation | `verdict`, `recommendation` | the recommended `repo` (when there is one), the `reason`, and the `next` step |
| Coverage summary | `coverage` | `must_have` count, and for the recommended pick `covered` and `missing`; `found_in` says for each covered one whether the evidence is in the `readme` or only in the repository's `metadata` (name, topics, description); `unknown` when the verdict is `inconclusive` |
| Gate results | `picks[].gates`, `rejected[]` | each gate's finding for every pick; for rejections the gate and reason, with `unchecked: true` when the check could not complete |
| Unresolved checks | `unresolved_checks`, `picks[].unresolved_checks` | what could not be established, for the component and for each pick |
| Integration risks | `picks[].integration_risks` | capability gaps first, then licence obligations, maintenance, release and CI risks |

`also_passed` lists projects that passed every gate but ranked below the picks, and `constraints` echoes
the constraints applied, including defaults the caller did not set.

**What a result never says.** No gate, score or verdict claims a project is safe, secure or fully
compatible. The gates do not read source code, scan dependencies or run anything, and coverage is read
from text the repository owner wrote. Every result carries a `limits` line saying so, and the skill and
server instructions tell agents not to make that claim either.

## Security gates (fail closed)

Any failure removes the project; a check that could not complete counts as a failure. It is reported as
`unchecked` rather than as a finding, and a project kept out that way makes the verdict `inconclusive`
instead of `no_candidates`: failing closed keeps unverified projects out of the picks without turning
"could not check" into "nothing exists".

| Gate | Fails when |
| --- | --- |
| exists | the name does not resolve to a real **public** repository (invented names → `unresolved`) |
| status | archived, disabled, a fork, or no push in 730 days |
| license | no license, unrecognised (`NOASSERTION`), or outside the caller's policy (default permissive) |
| age | created less than 30 days ago (unless `allow_new`) |
| fit | primary language incompatible with the caller's project |
| vulns | a published high/critical advisory with no fixed version, or one that names no fixed version at all (fix status unknown), from repository advisories + GitHub Advisory Database; or the check could not complete: a source that errored, refused (403), was unavailable, or returned a list too long to read to its end (5 pages) |
| malware | a malware advisory for this repository |
| provenance | no commit could be pinned, a signature failed verification, or signatures are required and missing |
| lookalike | the name imitates a more established project and the repo is young or has few contributors |

READMEs are parsed into fixed-schema **capability cards**; text that tries to instruct an AI is detected
and dropped from the card, and the pick is flagged. The detector looks for instructions and for text
addressed to an AI reader, outside code blocks. Ordinary LLM vocabulary such as "tool calls" or "system
prompt" is not an instruction and does not trip it. See [SECURITY.md](SECURITY.md).

Two limits of the vulnerability check are worth knowing. The GitHub Advisory Database is searched by
package name and matched on the repository URL, so an advisory filed under a different package name is
not found, and one that cites no repository cannot be attributed: the pick then lists it under
`unresolved_checks` instead of counting or ignoring it. Upstream CI is reported as passing only when
every check run on the pinned commit was read (up to 300); otherwise it is reported as unknown.

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
