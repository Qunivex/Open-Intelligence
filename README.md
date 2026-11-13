# Open-Intelligence: Reuse Layer v1 (GitHub)

Reuse Layer tells coding agents **what to reuse before they build**. Ask it for a capability and it returns
the **top 3 open-source GitHub projects** that pass every security gate, each pinned to an exact tag and
commit SHA with evidence for the pick, or a clear **"build it yourself"** when nothing fits.

Each call decides **one component** of your project and returns that component's recommendation, its
coverage of your must-have capabilities, the gate results, the checks that stayed unresolved and the
integration risks. When it could not gather enough evidence to decide (rate limit, timeout, error) it says
`inconclusive` instead of pretending nothing exists. See [Verdicts and evidence](#verdicts-and-evidence).

v1 follows the *Reuse Layer: Build Plan* with one deliberate scope cut: **GitHub is the only source.**
The pipeline itself is source-neutral: everything specific to GitHub sits behind one adapter interface, so
another host (models, datasets) is added as an adapter without touching the pipeline. See
[the architecture and flow diagrams](docs/architecture.md), including
[how to add a source](docs/architecture.md#adding-a-source).

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
| `search` | Dependency of the discovery skill: the GitHub token lives in the server, so exploration has to go through it. | Compact results per query (name, about, topics, language, license, last push) |
| `report_outcome` | Dependency of the ranking: the track-record factor (20%) needs integration outcomes. | Acknowledgement and updated retention |

`find_reusable` and `search` take an `artifact_type`. Its allowed values are the artifact types that have
a source adapter, so the tool schema is the list of what can be searched; there is no registry to query
first. Today the only value is `repository` (public GitHub repositories), and it is the default.

Every pick has the same identity fields whichever source it came from: `provider`, `artifact_type`,
`name`, `url`, `version` (the pinned tag or branch), `version_kind` (`release`, `prerelease`, `tag` or
`branch`), `revision` (the commit SHA) and `license`. The source adds its own on top; for GitHub those
are `get` (the clone command), `ref_freshness`, `owner_tier`, `docs` and `package` (see
[Two ways to take a pick](#two-ways-to-take-a-pick-registry-package-or-github-source)).

The prompt **`discover_reusable`** carries the discovery workflow to any MCP client, including ones that
cannot load skills.

## Discovery: intent, not keywords

Splitting a request into keywords finds projects that share its *words*, not its *meaning*. The
[`reuse-discovery` skill](src/reuse_layer/integrations/skills/reuse-discovery/SKILL.md) has the
agent:

1. write an **intent brief**: the job to be done, the kind of artifact, constraints, and an ambiguity
   check that turns other meanings of the same words into `avoid` phrases;
2. decompose it into **capabilities**, each with practitioner phrasings (`token counting | tokenizer`);
3. generate **hypotheses** through several lenses: direct solutions, projects that *embed* the capability,
   upstream primitives, standards and formats, adjacent domains, awesome-lists, vendor orgs;
4. build a **query portfolio** of differently angled searches (practitioner phrase, `topic:`,
   `in:readme` phrase, data/artifact, language-scoped, `NOT` the wrong sense);
5. explore with `search`, then refine once from the vocabulary of the good hits;
6. hand everything to `find_reusable`, one call per component, which verifies, gates and ranks it,
   pinning versions the way the user chose when asked once (`ref_policy`: standard releases or everything);
7. show the user a short **discovery trace** and the decision: the interpreted job, the must-have
   requirements, the constraints applied, the searches used, the candidate shortlist and what happened to
   each name, then the verdict with its coverage, gate results, unresolved checks and integration risks,
   and for each pick both ways to take it (registry package or GitHub source) for the user to choose.
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
whether the branch head is newer and by how many days. Commit freshness is reported as evidence and does not
currently change the ranking score.

## Which version is pinned (`ref_policy`)

Which versions are acceptable is the user's choice. The skill asks once per conversation and passes the
answer as `context.ref_policy` (CLI: `--ref-policy`); `constraints.ref_policy` echoes it back.

| `ref_policy` | Pins | Then | Last resort |
| --- | --- | --- | --- |
| `standard` (default) | the highest **stable** release whose tag is a plain version: `v1.2.3`, `1.2.3`, `1.2`, or the project's own name and a version (`tokio-1.37.0`, `ai@3.4.0`) | the highest such tag | the default branch head |
| `all` | the highest version of **any** kind: pre-releases (`2.0.0-rc.1`) and any naming scheme (`pkg@1.2.3`, `release-1.2.3`); a rolling release with no version (`nightly`) only when nothing else has one | the highest version tag of any kind | the default branch head |

Under `standard`, a major-only tag (`v1`) is not a version: such tags are usually moved to each new
release. A sub-package's tag in a monorepo (`tokio-macros-2.2.0` in `tokio`) is not the project's version
either. Every pick says what it got in `version_kind`. A `branch` pin of a project that does tag versions,
just none the policy accepts, carries an integration risk saying so; a `prerelease` pin carries one too.
Whatever the policy, the pin is always an exact commit (`revision`), never a moving branch.

## Two ways to take a pick: registry package or GitHub source

After the decision, each pick is looked up on its language's official package registry at the pinned
version, so the user can choose between installing the published package and using the source from
GitHub. It is informational: it never changes a pick, a gate, a score or the verdict.

| Language | Registry | Manifest read at the pinned commit | `install` | Build attestation read |
| --- | --- | --- | --- | --- |
| Python | PyPI | `pyproject.toml`, `setup.cfg`, `setup.py` | `pip install name==1.2.3` | PEP 740 attestation (Trusted Publisher) |
| JavaScript, TypeScript | npm | `package.json` | `npm install name@1.2.3` | SLSA provenance |
| Rust | crates.io | `Cargo.toml` | `cargo add name@=1.2.3` | trusted-publishing record |
| Go | Go module proxy | `go.mod` | `go get module@v1.2.3` | none kept |
| Ruby | RubyGems | `*.gemspec` | `gem install name -v 1.2.3` | Sigstore attestation |
| PHP | Packagist | `composer.json` | `composer require vendor/name:1.2.3` | none kept |
| Java, Kotlin, Scala, Groovy | Maven Central | `pom.xml`, or `GROUP` and `POM_ARTIFACT_ID` in `gradle.properties` | the coordinates `group:artifact:1.2.3`, for `pom.xml` or `build.gradle` | not read (see below) |
| C#, F#, Visual Basic | NuGet | `*.csproj`, `*.fsproj`, `*.vbproj`, `*.nuspec` | `dotnet add package Name --version 1.2.3` | none kept |

A project in another language is checked against the first of these manifests found at its root, if any.
The package name comes from the manifest; the version is the one the pinned tag names, else the one the
manifest declares (a Maven `-SNAPSHOT` version is never looked up: Central does not hold snapshots).

**Monorepos.** When the root manifest is a workspace (a private `package.json`, a Cargo `[workspace]`, a
Maven aggregator POM, a `gradle.properties` that only sets the group), when there is none (a .NET
solution, a Python package in `python/`), or when the pinned tag names a package (`@scope/core@1.2.3`,
`core-v1.2.3`, Go's `sub/v1.2.3`), the pinned commit's file tree is searched (one GitHub request) for
manifests up to four directories deep, skipping tests, examples, samples, docs and vendored code. The one
named after the tag's package, else after the repository (also without a language suffix: `sigstore-js` ->
`sigstore`), is checked; when there is exactly one, it is used. Members inherit what their workspace root
sets (a Cargo workspace version, a Maven or Gradle group). `package.path` names the directory, and a
package that cannot be singled out is `unknown`, with the reason.

`package.status` is:

| Status | Meaning |
| --- | --- |
| `published` | The registry has the package at that version **and** links it to this repository: its metadata names it as the source, or its build attestation does. `install` is given. `same_commit` is `true` or `false` when the registry records the commit it was built from (npm, Go, Packagist, NuGet, crates.io and RubyGems attestations, a Maven POM whose `<scm><tag>` is a commit). |
| `not_linked` | The registry has a package with that name and version, but nothing links it to this repository, or its build attestation names another one. Anyone can register a name, so it may be a different project: it is never offered. |
| `yanked` | The maintainers withdrew that version from the registry (NuGet: unlisted it). Not offered. |
| `not_published` | The registry has no such version. |
| `unknown` | It could not be checked: no package could be singled out in a monorepo, a Gradle build that sets its coordinates in code, a branch pin with no declared version, a registry or GitHub that did not answer. `detail` says which. |
| `unsupported` | No supported manifest at the repository root, in a language with no registry above. |

`package.attested` is `true` when the registry holds a build attestation naming this repository: the record,
made at publish time from the CI workflow's identity, of the repository the version was built from.
`false` means the registry keeps such records (npm, PyPI, crates.io, RubyGems) and has none for this version;
absent means it keeps none, or it could not be read. Attestations are read, not re-verified: no signature is
checked here, so `attested` reports the registry's own record (npm and PyPI verify attestations on upload).
Maven Central's optional Sigstore bundles are not read, because Central does not verify them.

The metadata links behind `published` are claims: the manifest is written by the repository owner and the
registry metadata by the publisher. Two claims that agree are good evidence, not proof; an attestation and
`same_commit` are stronger.
Turn the check off with `[packages] check = false` in the config file.

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
| Recommendation | `verdict`, `recommendation` | the recommended `repo` (its name, when there is one), the `reason`, and the `next` step |
| Coverage summary | `coverage` | `must_have` count, and for the recommended pick `covered` and `missing`; `found_in` says for each covered one whether the evidence is in the `readme` or only in the repository's `metadata` (name, topics, description); `unknown` when the verdict is `inconclusive` |
| Gate results | `picks[].gates`, `rejected[]` | each gate's finding for every pick; for rejections the gate and reason, with `unchecked: true` when the check could not complete |
| Unresolved checks | `unresolved_checks`, `picks[].unresolved_checks` | what could not be established, for the component and for each pick |
| Integration risks | `picks[].integration_risks` | capability gaps first, then licence obligations, maintenance, release and CI risks |
| How to take it | `picks[].version_kind`, `get`, `package` | what the pin is, the clone command, and whether the language's registry publishes it at that version |

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

Layout:

```
src/reuse_layer/
  config.py, config.example.toml   the user config file and its template
  core/        engine (the source-neutral pipeline), models, ranking, relevance, store, text;
               gates, signals, cards, discovery, licenses (checks the GitHub adapter is built from)
  sources/     base.py (the Source contract), github.py (REST client + adapter),
               packages.py (registry lookup of the picks: PyPI, npm, crates.io, Go, RubyGems, Packagist,
               Maven Central, NuGet; monorepo search; build attestations),
               __init__.py (build_sources: where adapters are registered), registry.toml (curated owners)
  adapters/    mcp_server.py, cli.py
  integrations/skills/reuse-discovery/SKILL.md
tests/         offline tests against a fake GitHub API
```

## Not in v1 (from the build plan)

Other sources to search (PyPI, npm, Hugging Face, government and research catalogs; v1 only looks a
GitHub pick *up* on its registry), verifying registry attestation signatures (they are read, not
re-verified), coordinates computed by Gradle build scripts, deps.dev/ecosyste.ms
dependents and one-hop trust, OSV and OpenSSF Scorecard, sandbox verification, `compare`/`verify`/
`check_dependency`, the dependency hook and GitHub Action, the hosted HTTP server with OAuth, and
learning-to-rank. A new source is added as an adapter behind `sources/base.py`; see
[Adding a source](docs/architecture.md#adding-a-source).
