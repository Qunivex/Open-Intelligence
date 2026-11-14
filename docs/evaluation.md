# Evaluation

Does the ranking put better candidates first than each platform's own order? This page describes how
that is measured, and what the first measurement showed.

**Short version (2026-11-14, seed set, 8 tasks):** the ranking scores slightly higher on average than the
platforms' own order (nDCG@3 0.57 against 0.51) but wins only 3 of the 8 tasks and loses 5. That is not
yet evidence that the ranking is better. The set is small, it was graded by the assistant that built the
ranking, and two fixes were made after seeing the first result. Read the numbers as a list of what to fix
next, not as a score.

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
`--no-ablations` skips the five extra runs. A live run of the seed set took under two minutes. Hugging
Face's dataset viewer rate-limits anonymous callers (it did during this work), so set `HF_TOKEN` for
repeated runs. A recording of the seed set is about 3.5 MB and is not kept in the repository.

To change the weights, set them under `[ranking.weights]` in the config file and replay a recording.

## The seed set, and why not to trust it yet

Ten tasks: four models, four datasets, two code. The two code tasks need a GitHub token and were skipped,
so **nothing below says anything about repositories**, including the OpenSSF Scorecard and vulnerability
evidence.

- The grades were written by the AI assistant that built the ranking, from general knowledge of the
  fields and from what the searches returned. That is a conflict of interest.
- Eight scored tasks is far too few to separate a real difference from noise.
- Two changes to the ranking were made after the first run and measured again on the same tasks (see
  "How the number got here"). A result measured on the tasks that prompted the change is optimistic.

The project needs 30 to 50 tasks per type, graded by practitioners who have not seen the ranking's output.

## First results

Data as of 2026-11-14. Weights: fit 40%, quality 20%, health 15%, trust 15%, adoption 10%.

| task | need | ranking nDCG@3 | best platform order nDCG@3 | ranking P@3 | judged@3 |
| --- | --- | ---: | ---: | ---: | ---: |
| sentiment-english-model | model | 0.40 | 0.16 | 0.67 | 1.00 |
| speech-recognition-english-model | model | 0.60 | 0.87 | 1.00 | 1.00 |
| sentence-embeddings-english-model | model | 1.00 | 0.30 | 1.00 | 1.00 |
| german-ner-model | model | 0.28 | 0.40 | 0.33 | 1.00 |
| german-ner-dataset | dataset | 0.88 | 0.16 | 0.67 | 1.00 |
| movie-review-sentiment-dataset | dataset | 0.47 | 0.51 | 0.33 | 0.33 |
| question-answering-dataset | dataset | 0.50 | 0.70 | 0.33 | 0.67 |
| customer-churn-dataset | dataset | 0.40 | 1.00 | 0.33 | 1.00 |
| python-http-client | code | skipped | | | |
| python-data-validation | code | skipped | | | |
| **mean** | | **0.57** | **0.51** | **0.58** | |

Mean precision@3 of the best platform order: 0.50.

### Where it wins

- **Sentence embeddings (1.00 against 0.30).** Searching by name returns models with "sentence" in their
  name; the standard ones (`all-MiniLM-L6-v2`, `nomic-embed-text-v1.5`, `bge-base-en-v1.5`) have no such
  word in their name. They are found by browsing the Hub's "sentence similarity" task.
- **German NER dataset (0.88 against 0.16).** The phrasing "germeval" finds the standard benchmark, and
  the platforms' first results are small community uploads.
- **English sentiment model (0.40 against 0.16).** Both are weak; the platforms lead with a
  financial-news model and its conversion.

### Where it loses

- **Customer churn dataset (0.40 against 1.00).** Kaggle's own first result is the dataset everyone uses
  (`blastchar/telco-customer-churn`). The ranking prefers re-uploads of the same data: they are newer and
  carry a clean license label, while the original says "Data files © Original Authors" and was last
  updated in 2018. Penalizing an unclear license rewards whoever re-uploaded the data under a label they
  may have had no right to choose.
- **English speech recognition (0.60 against 0.87).** The Hub leads with `whisper-large-v3` and its turbo
  version. The ranking puts `whisper-small` and `whisper-base` ahead of them: the small models publish
  eval results in their cards, the large ones do not, so the small ones look better evidenced.
- **Question answering dataset (0.50 against 0.70).** `squad_v2` is first, but a multiple-choice benchmark
  that is merely filed under "question answering" is second and plain `squad` misses the top 3.
- **German NER model (0.28 against 0.40).** The standard models (`flair/ner-german-large`,
  `flair/ner-german`) ship only pickle weights, so the gate removes them.
- **Movie review dataset (0.47 against 0.51).** `stanfordnlp/imdb` is first; the next two picks are a film
  metadata table from Kaggle and an ungraded re-upload. Word matching cannot tell "movie reviews" from
  "movies with review counts".

### Which parts of the score earn their weight

| score part removed | mean nDCG@3 without it | change |
| --- | ---: | ---: |
| fit | 0.29 | -0.28 |
| quality | 0.56 | -0.01 |
| health | 0.57 | +0.01 |
| trust | 0.50 | -0.07 |
| adoption | 0.52 | -0.05 |

A negative change means the part helps. On this set fit does most of the work, trust and adoption help a
little, and **quality (20%) and health (15%) change almost nothing**. Freshness in particular does not
separate good models and datasets from poor ones: the standard choices are often years old and stable.

Three other weightings were tried on the same recorded data (before the second fix below), each moving
weight from health to adoption. All landed within 0.01 of the default. The weights are not what holds the
result back; the losses above are.

## How the number got here

The first run was not the number above. In order:

| run | change | ranking nDCG@3 | best platform order |
| --- | --- | ---: | ---: |
| 1 | as first built, with task browsing by "most downloaded" and "trending" | 0.39 | 0.51 |
| 2 | "trending" removed: it filled the list with that week's uploads | 0.49 | 0.51 |
| 3 | a candidate that matches only the task category keeps 75% of its fit | 0.57 | 0.51 |

Both changes are defensible on their own terms, and both were chosen after looking at failures on these
same eight tasks. Expect a fresh set to score lower than 0.57.

## What to do next, in order of expected effect

1. **A real golden set.** Nothing below can be trusted without it.
2. **Run the code tasks** with a GitHub token. The repository side has never been evaluated.
3. **Stop rewarding re-uploads.** Find the original of a dataset (same files, earlier date, more notebooks)
   and rank it, not its copies; treat a clean license on a copy of unclearly licensed data as unclear.
4. **Reconsider the pickle-only gate for widely used models**: a warning instead of a removal above some
   level of adoption, or when the publisher is the original lab.
5. **Do not let "has eval results in its card" outrank a better model without them**: compare within a
   model family, or use a task leaderboard.
6. **Replace freshness for models and datasets** with "has it been superseded", or drop it.
7. **Judge fit by meaning.** Several losses come from matching words rather than what the candidate is.
