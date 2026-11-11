# Security model (v1, GitHub)

Reuse Layer becomes a trust point for many agents, so it assumes adversarial repositories, READMEs and
users. This file maps the threats in the build plan to what v1 does.

## Credentials

| Rule | How v1 enforces it |
| --- | --- |
| Agents never see a key | No MCP tool input or output has a key, token or secret field (tested). Errors returned to agents are generic. |
| Only a person adds keys | Keys come only from the user's config file, an environment variable it names, or the OS keychain (`reuse-layer keys set`, which prompts without echo). |
| Keys never live in the repo | The config file lives in the per-user config directory. Inline keys are **refused** when the config file sits inside a git work tree. `.gitignore` excludes `config.toml`, `.env*` and the SQLite store. On POSIX the file is created `0600`, with a warning if it is readable by others. |
| Keys never leak | Keys are wrapped in `Secret` (masked `repr`/`str`); a logging filter scrubs them from all log output; the token is sent only in the `Authorization` header and never in a URL, cache key or response. |
| Least privilege | `reuse-layer doctor` warns when a classic token carries write or private scopes. v1 needs only a fine-grained token with public, read-only access. |
| The token stays on GitHub | Redirects are followed by hand and only to the configured API host, so the header is never sent elsewhere. The API URL must be `https://`. |

**Caveat:** a coding agent with shell access can read any file your user account can read, including the
config file. If that matters to you, use `{ keyring = "reuse-layer" }`.

## Threats and mitigations

| Threat | Mitigation in v1 |
| --- | --- |
| Ranking manipulation (bought stars, sockpuppets) | Stars carry no weight (tie-break only). Trust comes from contributors, bus factor, releases, recency, age and community health. Young projects with star bursts and few contributors are held in **probation** (explore slot only). Owner tier comes from a curated registry and GitHub org verification. |
| Prompt injection in READMEs or descriptions | Fetched text is sanitised (control, zero-width and bidi characters removed, length capped) and marked untrusted in every response. Judges read fixed-schema capability cards, not raw READMEs. Injection-like text drops the README from the card and flags the pick. No model output can override a gate. |
| Invented or lookalike names | Every name is resolved against GitHub; unresolved names are reported and never become picks. Near-identical names imitating a more established project fail the lookalike gate when young or thinly maintained. Forks fail by default. |
| Vulnerable or malicious projects | Repository security advisories and the GitHub Advisory Database (matched on the repository URL) are checked; unfixed high/critical advisories and malware flags fail the gate. A check that cannot complete **fails closed**: the project is kept out of the picks and reported as `unchecked`, which is not the same as a finding against it. An advisory whose fix status is unknown is never reported as fixed. |
| Over-trusting a result | No gate, score or verdict says a project is safe, secure or fully compatible: the gates do not read source code, scan dependencies or run anything. Every result carries a `limits` line, `unresolved_checks` lists what could not be established, and the skill and server instructions forbid agents from making the claim. |
| Missing evidence read as "nothing exists" | A failed or rate-limited search, an unverified candidate, an evidence pass that timed out and a check that could not complete are decisive gaps. With any of them the verdict cannot be `no_candidates` or `build_yourself`; it is `inconclusive`, and building is not recommended. |
| A trusted or popular project hiding a capability gap | Picks are ordered by must-have coverage before score, `reuse` needs evidence for every must-have, and a model judge cannot turn a gap into coverage. Coverage is read from text the repository owner wrote, so it is reported as evidence ("shows evidence for"), not as fact; a project that overstates itself still has to pass every gate, and young or fast-rising ones stay limited to the single explore slot. |
| Takeover or tampered releases | Every pick is pinned to a tag **and** its commit SHA; signatures that fail GitHub verification fail the gate; `require_signed_releases` makes unsigned releases fail too. |
| Leaking private code or plans | Searches always carry `is:public`; private or internal repositories are dropped before caching or returning. Tool inputs carry task text and constraints, never source files. The ledger stores a hash of each task, not its text, unless `retain_task_text = true`. |
| Poisoned outcome reports | An outcome must name a recommendation this store issued and one of its picks; one report per (recommendation, repo, outcome); 30 reports a minute at most. |
| Abuse of GitHub's API | Per-call time budgets, an overall deadline, a search throttle under GitHub's per-minute limit, primary and secondary rate-limit handling, and ETag caching. |
| The layer as a single point of failure | Evidence with every pick; the layer never installs anything; gates fail closed, while an unavailable layer returns a generic error so the agent can continue. |

## Out of scope for v1

Sandbox verification of picks, OSV/OpenSSF Scorecard signals, maintainer-change detection, and the
hosted multi-tenant server (OAuth 2.1, per-user budgets, audit log) arrive in later phases.

## Reporting a vulnerability

Please report security issues privately through GitHub's "Report a vulnerability" on this repository, not
in a public issue.
