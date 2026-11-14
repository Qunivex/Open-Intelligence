# Ranking parameters: research notes

Status: **research, not implemented.** The MVP finds and fetches candidates and returns them in each
platform's own order. This document collects the parameters worth researching before we pick the top 3
open-source projects, models and datasets for a use case, and how to test whether a ranking is any good.

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

## 10. How to tell whether a ranking is good

- **A golden set.** 30 to 50 real tasks per type (repository, model, dataset), each with picks that
  practitioners judged acceptable, good or best.
- **Metrics.** nDCG@3 and precision@3 against the golden set; "would build on it" rate from a human review.
- **Baseline.** Each platform's own popularity sort. A ranking that does not beat it is not worth the
  complexity.
- **Ablations.** Remove one parameter at a time to find out which ones actually change the top 3.
- **Outcomes.** After an agent builds on a pick, record whether it worked. Over time this is the most
  honest signal there is.

## 11. Open questions

- Which task-specific benchmark sources can be trusted and queried, now that leaderboards are fragmented?
- How to normalize adoption fairly between a niche task (hundreds of downloads) and a popular one (millions)?
- How to detect inflated popularity on Hugging Face and Kaggle, as StarScout does for GitHub stars?
- How much of fit can be judged from metadata alone, and when must the agent read the README or card?
- How to weigh a license that is unclear rather than clearly incompatible?
- How should the user's constraints be collected: asked up front, or inferred from their project files?

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
