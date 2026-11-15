# Evaluation

Does the ranking put better candidates first than each platform's own order? This page describes how
that is measured, and what the measurements so far show.

**Short version (data of 2026-11-14, seed set, 10 tasks):** the ranking scores somewhat higher on average
than the platforms' own order (nDCG@3 0.66 against 0.56). It wins 3 tasks, ties 3 and loses 4. That is
not yet evidence that the ranking is better. The set is small, it was graded by the assistant that built
the ranking, and several fixes were made after seeing earlier results on these same tasks. Read the
numbers as a list of what to fix next, not as a score.

## How it works

1. **A golden set** lists real tasks. Each task has the search phrasings, the must-haves and constraints
   an agent would pass, and graded judgments of candidates: 3 = the standard choice, 2 = a good choice,
   1 = usable with caveats, 0 = off-task, broken or not usable. A candidate without a grade counts as 0.
2. **The runner** ranks every task and scores the top 3 against the judgments:
   - **nDCG@3**: how close the top 3 is to the best possible top 3 (1.0 is ideal). It rewards putting the
     best candidates first.
   - **precision@3**: the share of the three slots filled with a good pick (grade 2 or 3).
   - **judged@3**: the share of the top 3 that has a grade at all, so a low score can be told apart from
     an unjudged one.
3. **The baseline** is what each platform showed first for the first search phrasing, in its own default
   order. The report uses the better of the platforms for each task, which is generous to the baseline: a
   user would have to know which platform to look at.
4. **Ablations** re-run the set with each part of the score switched off in turn, to see which parts
   change the top 3 and in which direction.

Live results change from day to day. The runner can record every upstream answer and replay it, so the
same data can be scored again by different code or weights.

## Running it

```bash
open-intelligence eval
```

```bash
open-intelligence eval --record run.json.gz
```

```bash
open-intelligence eval --replay run.json.gz --json
```

`--golden path.toml` uses your own golden set; without it the seed set shipped with the package is used
([seed.toml](../src/open_intelligence/evaluation/seed.toml), which also documents the file format).
`--no-ablations` skips the five extra runs. A live run of the seed set takes a few minutes. Hugging Face's
dataset viewer rate-limits anonymous callers (it did during this work), so set `HF_TOKEN` for repeated
runs. A recording of the seed set is several megabytes and is not kept in the repository.

To change the weights, set them under `[ranking.weights]` in the config file and replay a recording.

## The seed set, and why not to trust it yet

Ten tasks: four models, four datasets, two code.

- The grades were written by the AI assistant that built the ranking, from general knowledge of the
  fields and from what the searches returned. That is a conflict of interest.
- Ten tasks is far too few to separate a real difference from noise.
- Six changes to the ranking were made after seeing results on these tasks (see "How the number got
  here"). A result measured on the tasks that prompted the change is optimistic.

The project needs 30 to 50 tasks per type, graded by practitioners who have not seen the ranking's output.

## Results

Data as of 2026-11-14. Weights: fit 40%, quality 20%, health 15%, trust 15%, adoption 10%.

| task | need | ranking nDCG@3 | best platform order nDCG@3 | ranking P@3 | judged@3 |
| --- | --- | ---: | ---: | ---: | ---: |
| sentiment-english-model | model | 0.40 | 0.16 | 0.67 | 1.00 |
| speech-recognition-english-model | model | 0.60 | 0.87 | 1.00 | 1.00 |
| sentence-embeddings-english-model | model | 1.00 | 0.30 | 1.00 | 1.00 |
| german-ner-model | model | 0.28 | 0.40 | 0.33 | 1.00 |
| german-ner-dataset | dataset | 0.88 | 0.16 | 0.67 | 1.00 |
| movie-review-sentiment-dataset | dataset | 0.51 | 0.51 | 0.33 | 0.67 |
| question-answering-dataset | dataset | 0.50 | 0.70 | 0.33 | 0.67 |
| customer-churn-dataset | dataset | 1.00 | 1.00 | 1.00 | 1.00 |
| python-http-client | code | 0.73 | 0.83 | 1.00 | 1.00 |
| python-data-validation | code | 0.67 | 0.67 | 0.33 | 0.33 |
| **mean** | | **0.66** | **0.56** | **0.67** | |

Mean precision@3 of the best platform order: 0.53.

### Where it wins

- **Sentence embeddings (1.00 against 0.30).** Searching by name returns models with "sentence" in their
  name; the standard ones (`all-MiniLM-L6-v2`, `nomic-embed-text-v1.5`, `bge-base-en-v1.5`) have no such
  word in their name. They are found by browsing the Hub's "sentence similarity" task.
- **German NER dataset (0.88 against 0.16).** The phrasing "germeval" finds the standard benchmark, and
  the platforms' first results are small community uploads.
- **English sentiment model (0.40 against 0.16).** Both are weak; the platforms lead with a
  financial-news model and its conversion.

### Where it ties

- **Movie review dataset (0.51 each).** `stanfordnlp/imdb` is first in both. The ranking's other two picks
  are a small community dataset and a film metadata table: word matching cannot tell "movie reviews" from
  "movies with review counts".
- **Python data validation (0.67 each).** `pydantic` is first in both; neither fills the other two slots
  with a graded candidate.

- **Customer churn dataset (1.00 each).** This was the biggest loss (0.40 against 1.00). Copies of a
  dataset are now recognized by their content (the same column names, the same file size, or the same
  width with a related name) and fold under the original, so `blastchar/telco-customer-churn` leads
  14 uploads of the same data instead of competing with them. It is first by a narrow margin over the
  classic bank churn file, which leads 11 uploads of its own.

### Where it loses

- **English speech recognition (0.60 against 0.87).** The Hub leads with `whisper-large-v3` and its turbo
  version. The ranking puts `whisper-small` and `whisper-base` ahead of them: the small models publish
  eval results in their cards, the large ones do not, so the small ones look better evidenced.
- **Python HTTP client (0.73 against 0.83).** All three picks are good (`urllib3`, `requests`, `aiohttp`),
  but GitHub's own order leads with `httpx`, which the ranking leaves fourth.
- **Question answering dataset (0.50 against 0.70).** `squad_v2` is first, but a multiple-choice benchmark
  that is merely filed under "question answering" is second and plain `squad` misses the top 3.
- **German NER model (0.28 against 0.40).** The standard models (`flair/ner-german-large`,
  `flair/ner-german`) ship only pickle weights, so the gate removes them.

### Which parts of the score earn their weight

| score part removed | mean nDCG@3 without it | change |
| --- | ---: | ---: |
| fit | 0.39 | -0.26 |
| quality | 0.65 | -0.00 |
| health | 0.67 | +0.01 |
| trust | 0.62 | -0.04 |
| adoption | 0.45 | -0.21 |

A negative change means the part helps. On this set **fit and adoption do the work**: removing either
costs a third of the score. Adoption carries only 10% of the weight and matters as much as fit at 40%.
Quality (20%), health (15%) and trust (15%) change almost nothing.

## How the number got here

The number above is the sixth measurement, not the first. In order:

| run | tasks | change | ranking nDCG@3 | best platform order |
| --- | ---: | --- | ---: | ---: |
| 1 | 8 | as first built, with task browsing by "most downloaded" and "trending" | 0.39 | 0.51 |
| 2 | 8 | "trending" removed: it filled the list with that week's uploads | 0.49 | 0.51 |
| 3 | 8 | a candidate that matches only the task category keeps 75% of its fit | 0.57 | 0.51 |
| 4 | 10 | code tasks added (GitHub token); copies of a dataset fold under the original, which is judged on the best evidence about the data but keeps its own license | 0.56 | 0.56 |
| 5 | 10 | an unclear license counts as neutral (0.5) instead of 0.3 | 0.62 | 0.56 |
| 6 | 10 | copies of a dataset are recognized by content (columns, file size, width), not only by name | 0.66 | 0.56 |

Each change is defensible on its own terms, and each was chosen after looking at failures on these same
tasks. Expect a fresh set to score lower than 0.66.

On run 5: the choice to penalize and flag an unclear license stands. Its size was the question. At 0.3 an
unclear license scored below a license that is known to forbid commercial use, and cost the standard churn
dataset ten points against re-uploads with a tidier label. Three sizes were measured on the same data:

| unclear license counts as | ranking nDCG@3 | churn task |
| ---: | ---: | ---: |
| 0.3 | 0.56 | 0.16 |
| 0.5 | 0.62 | 0.62 |
| 0.7 | 0.65 | 0.76 |

0.5 was chosen because it is what "unknown is neither good nor bad" means, not because it scored best:
0.7 scored higher, but would rank an unclear license above one known to restrict use.

Earlier, before runs 4 and 5, three other weightings were tried that moved weight from health to adoption.
All landed within 0.01 of the default.

## What to do next, in order of expected effect

1. **A real golden set.** Nothing here can be trusted without it.
2. **Check the copy matching on more data.** It was tuned on one task. Different versions and
   repackagings of a dataset count as copies, and so would two different datasets with the same
   three-word name.
3. **Give adoption more weight, and quality less**, once a real golden set confirms what the ablation
   shows here.
4. **Reconsider the pickle-only gate for widely used models**: a warning instead of a removal above some
   level of adoption, or when the publisher is the original lab.
5. **Do not let "has eval results in its card" outrank a better model without them**: compare within a
   model family, or use a task leaderboard.
6. **Judge fit by meaning.** Several losses come from matching words rather than what the candidate is.
