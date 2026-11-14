# Ranking parameters: research notes

Status: **a first version of the pipeline in section 9 is built** (`src/open_intelligence/ranking/`, tool
`find_top_picks`). It follows the decisions recorded in section 11. Its weights and thresholds are a first
hypothesis: the evaluation in section 10 that would calibrate them is **not built yet**. Section 13 lists
what the first version does, what it leaves out, and what running it against the live servers showed.

This document collects the parameters behind picking the top 3 open-source projects, models and datasets
for a use case, and how to test whether a ranking is any good.

Facts marked *(observed)* were seen on the live official MCP servers on 2026-11-13.

## 1. What the platforms rank by today

| platform | default order | other sorts offered by the official MCP | what that measures |
| --- | --- | --- | --- |
| GitHub | best match (text relevance) | stars, forks, help-wanted issues, updated | attention, accumulated over the repo's lifetime |
| Hugging Face | not documented in the tool | `trendingScore`, `downloads`, `likes`, `createdAt`, `lastModified` *(observed)* | momentum and request volume |
| Kaggle datasets | "hottest" (the Kaggle API's default) | `Hottest`, `Votes`, `Updated`, `Active`, `Published`, `Relevance`, `LastViewed`, `Usability`, `DownloadCount`, `NotebookCount` *(observed)* | recency-weighted engagement |
| Kaggle models | not documented | `Hotness`, `DownloadCount`, `VoteCount`, `NotebookCount`, `PublishTime`, `CreateTime`, `UpdateTime` *(observed)* | engagement |

None of these orders knows anything about *your* use case. Each also has a known distortion:

- **GitHub stars** never decay (an abandoned project keeps them), favour old projects, and can be bought.
  The StarScout study found about 6.0 million suspected fake stars and 18,617 repositories with suspected
  fake-star campaigns between July 2019 and December 2024, with a sharp rise in 2024 ([He et al.,
  ICSE 2026](https://arxiv.org/abs/2412.13459)). Most promoted short-lived malware or phishing repos, but
  some promoted AI/LLM and tool repositories.
- **Hugging Face downloads** are the last 30 days of HTTP requests (GET and HEAD) to a "query file",
  `config.json` by default ([Hub docs](https://huggingface.co/docs/hub/models-download-stats)). CI jobs,
  library defaults and re-downloads count. A model that a popular library loads by default racks up
  downloads without anyone choosing it. The `trendingScore` formula is not published.
- **Kaggle's usability rating** scores how complete a dataset's metadata is (description, license, file
  descriptions, tags...), not how good the data is. The formula is partly opaque; Chicco, Fabris and
  Jurman reverse-engineered its weights and proposed an alternative for biomedical data ([BioData Mining,
  2025](https://doi.org/10.1186/s13040-024-00412-x)).

What this looks like in practice *(observed)*:

- Hugging Face, models, query `sentiment`, default order: first hit is a transformers.js/ONNX conversion of
  a financial-news sentiment model (4.7K downloads). Right only if the user is in JavaScript and in finance.
- Kaggle Models, query `sentiment`: the first hit's own description says the code is experimental and
  "Not tested".
- Kaggle Datasets, query `customer churn`: the top hit (Telco Customer Churn) has 2,995 notebooks and a
  usability of 0.88, but its license reads "Data files © Original Authors" and it was last updated in 2018.
  Popular, well documented, legally unclear and old: four separate facts a single score would blur.

## 2. Principles

1. **Fit first.** A candidate that does not do the job is worthless however popular it is.
2. **Gates before scores.** Some facts disqualify (incompatible license, archived, known critical
   vulnerability, unsafe weights). Apply them as filters, not as score penalties that popularity can outweigh.
3. **Score within a type, never across types.** A repository, a model and a dataset are judged on
   different evidence. Don't compare their scores.
4. **Popularity is a damped prior, not the ranking.** Log-scale it, take percentiles within the candidate
   set (not across the whole platform), prefer recent velocity over lifetime totals, use Bayesian averaging
   for small counts, and check for anomalies (bursts of stars from low-activity accounts).
5. **Condition on the user's constraints.** Language, runtime, framework, license needs, hardware, data
   domain and size are inputs to ranking, not afterthoughts.
6. **Unknown is not bad and not good.** Track it explicitly ("no license found", "no eval results") and
   show it to the user.
7. **A diverse top 3.** Three fine-tunes of the same base model, or three forks of one library, is one
   option shown three times. Prefer distinct options (for example best fit, lightest, most mature).
8. **Every pick explains itself.** The parameters that put it there, and what was not checked.

## 3. Parameters shared by all three types

| parameter | why it matters | how to measure |
| --- | --- | --- |
| Task fit | the job must be done | capability coverage against the user's must-haves; README / card text vs the need; task tags (GitHub topics, HF `pipeline_tag`, Kaggle tags) |
| Constraint fit | a great pick in the wrong stack is useless | language, framework, runtime, OS, hardware, size, latency, offline use |
| License | decides whether it can be used at all | SPDX id; commercial use; model use-restriction licenses; dataset terms; "unknown" is its own state |
| Freshness | old work misses fixes and better methods | last release / commit / update; superseded by a newer version from the same publisher |
| Maintenance | someone fixes what breaks | release cadence, issue response time, active maintainers |
| Real adoption | usage beats attention | dependents, notebooks built on it, fine-tunes derived from it, downloads normalized within the task |
| Provenance | who made it, and is this the original | verified org, known lab or maintainer, original vs re-upload / mirror / conversion |
| Documentation | how fast the user can build on it | README / model card / dataset card completeness |
| Security | reuse imports risk | known vulnerabilities, unsafe formats, supply-chain hygiene |

## 4. Open-source projects (GitHub)

**Gates:** archived or marked deprecated; no license or an incompatible one; an unpatched critical
advisory in the latest release; malware or typosquat signals.

**Fit:** README feature match against must-have capabilities; topics; primary language; compatibility with
the user's framework and version (for example the Python versions it supports); working examples.

**Health** (see [CHAOSS](https://chaoss.community/) metrics): commit recency and cadence; release cadence
and semantic versioning; issue first-response time; issue close ratio; pull-request merge latency; number
of active maintainers in the last 12 months; contributor concentration (bus factor); company or foundation
backing.

**Security:** [OpenSSF Scorecard](https://github.com/ossf/scorecard) (18 checks such as Maintained,
Code-Review, Branch-Protection, Signed-Releases, Pinned-Dependencies, Dangerous-Workflow, Vulnerabilities;
pre-computed scores at `api.scorecard.dev`); known advisories (OSV, GitHub Advisory Database) for the
version you would install; how fast past advisories were fixed; signed releases and build provenance;
dependency footprint (count and depth of transitive dependencies).

**Real adoption:** dependents (deps.dev, ecosyste.ms); package downloads (PyPI, npm); stars only as a
log-damped prior, with star-velocity anomaly checks.

**Quality:** tests and CI passing on the default branch; type information; a docs site; a changelog;
frequency of breaking changes.

**Integration cost:** install size, transitive dependencies, API stability, how much glue code the user
still has to write.

## 5. Models (Hugging Face, Kaggle Models)

**Gates:** license incompatible with the intended use (non-commercial, acceptable-use restrictions);
pickle-only weights (`.bin`, `.pt`, `.pkl` can run code when loaded) without a clean scan, versus
`safetensors`; gated access the user cannot satisfy; does not fit the user's hardware even quantized.
Flag, rather than gate, models that need `trust_remote_code` (they run the repo's own code).

**Fit:** task (`pipeline_tag`), modality, languages, domain, library (`transformers`,
`sentence-transformers`, `diffusers`, GGUF / llama.cpp, ONNX, transformers.js), input and output format,
context length.

**Measured quality:** results on benchmarks that match the task (eval results in the model card or
`model-index`; task leaderboards such as MTEB for embeddings or speech-recognition leaderboards);
self-reported versus independently reproduced; benchmark contamination risk. Aggregated benchmark data has
fragmented: the original Open LLM Leaderboard was retired to an archive, and Papers with Code shut down in
July 2025 with Hugging Face's trending papers as the partial successor. Where to get trustworthy,
task-specific quality signals is an open research question for this project.

**Practical fit:** parameter count and memory (readable from `safetensors` metadata); quantized variants
(GGUF, AWQ, GPTQ); latency; availability on hosted inference providers.

**Lineage and ecosystem:** the `base_model` tree; how many fine-tunes, adapters and quantizations were
derived from it (people building on a model is a strong signal); Spaces using it. Prefer the original
publisher over re-uploads and conversions unless the conversion is what the user needs.

**Adoption:** 30-day downloads normalized within the same task and library, not across the Hub; likes.
Remember downloads are request counts.

**Publisher and documentation:** verified organization or known lab; model card covers intended use,
limitations, training data and evaluation.

**Freshness:** last modified; whether the same publisher has released a newer version that supersedes it.

**Kaggle Models specifics:** framework variations (Keras, PyTorch, JAX...), notebook count (Kaggle can
sort by it), downloads, whether the publisher is the original lab.

## 6. Datasets (Hugging Face, Kaggle)

**Gates:** unclear or incompatible license (a "© original authors" note is not a grant); personal data
without a consent basis; terms the user cannot accept; a re-upload when the original source is available.

**Fit:** task categories; modality; language; domain; schema, columns and label set match the user's
task; size in rows and bytes; available splits; time and geographic coverage; format (Parquet, CSV, JSON);
[Croissant](https://mlcommons.org/working-groups/data/croissant/) metadata (both Hugging Face and Kaggle
publish it), which makes schema matching automatable.

**Quality:** dataset card or datasheet completeness (how it was collected, how it was annotated, known
limitations); label noise and annotator agreement; duplicates; missing values; class balance; leakage
between splits; overlap with common benchmarks (contamination).

**Adoption:** on Kaggle, the number of notebooks built on it (`kernel_count`) shows people actually worked
with the data; on Hugging Face, models whose cards say they were trained on it; downloads, votes and likes;
citations.

**Freshness and maintenance:** last update, versions, whether questions in discussions get answers.

**Kaggle usability:** treat as a documentation score, not a quality score.

**Cross-source duplicates:** the same data often lives on both platforms and on the original site (UCI,
a government portal, a paper). Merge them and point to the original.

## 7. Notebooks and worked solutions (Kaggle)

Votes and medals; author tier; competition score or rank for competition notebooks; recency (old
notebooks pin old library versions); whether it ran and has output; forks; language. Competition
write-ups from top teams are reference designs in their own right. Note: Kaggle's notebook and
competition search needs a Kaggle token *(observed)*.

## 8. Candidate generation matters as much as scoring

Ranking can only reorder what the search returned, and all three platforms search mostly by keywords.
One query sorted by popularity misses good fits that use different words. Things to research:

- several queries per need, in the vocabulary practitioners use (synonyms, task names, library names);
- merging results from several sorts (relevance, downloads, recent) before ranking;
- filters the platforms already support (Hugging Face task and language tags, Kaggle file type and size);
- expansion: from a good base model to its fine-tunes; from a library to the projects that depend on it;
- de-duplication across sources and of mirrors and conversions.

## 9. A starting point to test, not a decision

1. Generate candidates as in section 8.
2. Apply the type's gates.
3. Score what passes, per type. A first hypothesis to calibrate against the evaluation below: fit 40%,
   evidence of quality 20%, health and maintenance 15%, trust and safety 15%, adoption 10%.
4. Pick a diverse top 3 and attach, to each pick, the parameters that put it there and the checks that
   could not be made.

Built as described: see section 13. The weights can be changed under `[ranking]` in the config file.

## 10. How to tell whether a ranking is good

Not built yet. Every result already carries `platform_top` (what the platform's own order put first), which
is the baseline this evaluation needs.

- **A golden set.** 30 to 50 real tasks per type (repository, model, dataset), each with picks that
  practitioners judged acceptable, good or best.
- **Metrics.** nDCG@3 and precision@3 against the golden set; "would build on it" rate from a human review.
- **Baseline.** Each platform's own popularity sort. A ranking that does not beat it is not worth the
  complexity.
- **Ablations.** Remove one parameter at a time to find out which ones actually change the top 3.
- **Outcomes.** After an agent builds on a pick, record whether it worked. Over time this is the most
  honest signal there is.

## 11. Decisions on the open questions

Decided by the project owner on 2026-11-13. Each is the first thing to revisit when the evaluation exists.

| question | decision | how it is built |
| --- | --- | --- |
| Which benchmark source for model quality? | The eval results a model's own card declares | `model-index` in the card is read; results verified by Hugging Face count more than self-reported ones; results mentioned only in the card's text count less; none counts least. Values are shown, not compared across models. |
| How to normalize adoption? | Rank within the candidates found | Percentile of each count among the candidates found on the same source for the same need. |
| How to handle inflated popularity? | Cross-check signals and damp | When independent signals disagree (many downloads, almost no likes; many stars, almost no forks) the adoption score is halved and the pick carries a warning. |
| How deeply to read for fit? | Read the shortlist | Every candidate is scored from search metadata; the best 8 per need and source get their README or card (and, for models, file list) read and are scored again. A second batch is read when gates remove most of the first. |
| An unclear license? | Penalize and flag | It stays eligible, loses trust score, and the pick lists the license as an unresolved check. A license that is clearly incompatible with the constraints is a gate. |
| How to collect constraints? | The agent infers them and asks only for gaps | `find_top_picks` takes `constraints`; the result lists every assumption made and, under `questions_for_user`, only the questions whose answer would change the result. |

Still open:

- How to detect inflated popularity with more than a ratio check (account age, velocity), as StarScout does.
- Whether adoption needs an absolute floor (see section 13, "what the live runs showed").
- Which task leaderboards to add once card evals prove too thin, and how to map a task to a leaderboard.
- How to judge fit by meaning instead of by words.

## 12. Where each signal comes from

| signal | through the connected official MCP servers | needs another source |
| --- | --- | --- |
| README, files, releases, tags | GitHub `get_file_contents`, `list_releases`, `list_tags` | |
| Security advisories | GitHub `list_global_security_advisories` | OSV.dev for other ecosystems |
| Scorecard, dependents | | api.scorecard.dev, deps.dev, ecosyste.ms |
| Package downloads | | PyPI and npm statistics |
| Model / dataset card, files, structure, preview | Hugging Face `hub_repo_details`, `hf_fs` | |
| Downloads, likes, trending, tags, license | Hugging Face `hub_repo_search` | |
| Fine-tunes derived from a model | Hugging Face search with `base_model:` filters (to verify) | Hub API |
| Kaggle dataset license, usability, notebooks, votes, size | Kaggle `search_datasets`, `get_dataset_info` | |
| Kaggle model variations, notebook count | Kaggle `list_models`, `get_model` | |
| Kaggle notebooks, competitions, write-ups | Kaggle `search_notebooks`, `get_writeup` (token needed) | |
| Task leaderboards and evals | partly in model cards | leaderboard datasets on the Hub |

## 13. The first version: what it does, what it leaves out, what it showed

Code: `src/open_intelligence/ranking/` (`adapters.py` reads each platform's results, `scoring.py` holds the
gates and scores, `engine.py` runs the pipeline). Tool: `find_top_picks`. Command: `open-intelligence picks`.

### What it does

**Candidates.** Up to three phrasings per need. For the first phrasing each source is asked twice: in its
default order and in one popularity order (GitHub stars, Hugging Face downloads, Kaggle notebook count), so
popular work is considered without being ranked by popularity. Results are merged and de-duplicated.

**Gates** (a failed gate removes the candidate; `rejected` in the result says which and why):

| gate | removes | needs |
| --- | --- | --- |
| archived | archived or disabled repositories | search metadata |
| license | non-commercial licenses when `commercial_use` is true; GPL-style when `copyleft_ok` is false | search metadata |
| age | anything older than `max_age_months` | search metadata |
| weights | models whose only weights are pickle files, unless `allow_pickle_weights` | the file list |
| size | models larger than `max_model_size_gb` | the file list |
| gated | models and datasets that need an access request, when `allow_gated` is false | a failed read |
| fit | fit below 0.3, or none of the must-haves covered | the README or card |

**Scores** (each 0 to 1; unknown counts as 0.5 and is listed under `unresolved_checks`):

| part | weight | repositories | models | datasets |
| --- | --- | --- | --- | --- |
| fit | 40% | words of the must-haves (or of the best-matching phrasing) found in name, description, topics, README; programming language | same, in name, tags, task, card; ML library; natural language | same; natural language |
| quality | 20% | README length, install and usage instructions, releases, signs of tests or CI | eval results in the card (verified, self-reported, prose only, none); card sections | card sections and metadata (Hugging Face); usability rating and description (Kaggle) |
| health | 15% | time since last push and since last release (half-life 18 months) | time since last update (30 months) | time since last update (60 months) |
| trust | 15% | license kind; fork or original | license kind; original or conversion; safetensors / GGUF / ONNX weights; custom code | license kind |
| adoption | 10% | stars, forks | downloads in 30 days, likes (Hugging Face); votes (Kaggle) | downloads, likes (Hugging Face); notebooks, downloads, votes (Kaggle) |

**Top 3.** Best score first. A conversion or adapter of a model, a fork, or a same-named re-upload is not a
separate option: it is listed under the pick it duplicates (`also_available_as`). Two fine-tunes of one base
model can both appear, with a small penalty for the second.

**Output per pick.** Score and its five parts, the reasons behind them, must-haves covered and missing,
facts (license, dates, size, eval results), warnings, unresolved checks, and where the platform's own order
had it.

### What it leaves out

- **GitHub is not verified against the live server.** That needs a token. Its parser follows the result
  types in GitHub's documentation and is tested against hand-written fixtures only.
- **Repositories:** no OpenSSF Scorecard, security advisories, dependents or package downloads (they need
  sources other than the three MCP servers, or a package name). Issue response times and contributor
  counts are not read. Tests and CI are inferred from the README.
- **Models:** eval values are shown but not compared between models. No benchmark-contamination check, no
  "superseded by a newer version" check. Kaggle does not report weight formats.
- **Datasets:** nothing about label quality, duplicates, personal data or schema match. Croissant metadata
  is not read.
- **Notebooks:** searched and listed by votes only, with field names assumed from Kaggle's API; this path
  needs a Kaggle token and has not been run live.
- **Candidate generation:** platform filters and expansion (a base model's fine-tunes, a library's
  dependents) are not used. Duplicates across platforms are caught by name only.
- **Evaluation** (section 10): no golden set, no metrics. The weights are unproven.

### What the live runs showed (2026-11-13)

- **Several phrasings matter more than any score.** For a German NER dataset, Hugging Face's first three
  results for "german ner" were community datasets with 14 to 62 downloads. With the phrasings "german ner",
  "german named entity recognition" and "germeval", the top pick was `GermanEval/germeval_14`, the standard
  benchmark, which the first search does not return at all.
- **Conversions crowd the default order.** Hugging Face's first result for "sentiment" was a JavaScript
  conversion of a financial-news model. The pipeline lists it under the original instead.
- **The inflation check fires on real data.** A sentiment model with 3.4M downloads in 30 days and 13 likes
  was flagged; the most-downloaded honest model in the same list has 2.4M downloads and 857 likes.
- **Ranking adoption only within candidates lets a weak source place a pick.** Kaggle Models candidates for
  "sentiment" have 4 to 8 votes each, so a model with 8 votes scores "typical" and can reach the top 3. An
  absolute floor (the "blend of both" option) would fix this; adoption is 10% of the score, which bounds it.
- **The pickle-only gate removes known, widely used older models** (for example
  `j-hartmann/sentiment-roberta-large-english-3-classes`). That is the gate working as specified; the result
  lists them under `rejected` and `allow_pickle_weights` brings them back.
- **Fit is lexical.** A financial-news sentiment model covered the must-haves "sentiment classification" and
  "english" for a product-review task. Must-haves have to name the domain ("product reviews") to exclude it.
- **Hugging Face answers in Markdown written for a model to read.** The parsers for it are the part most
  likely to break; `pytest -m live` checks them against the real server.
