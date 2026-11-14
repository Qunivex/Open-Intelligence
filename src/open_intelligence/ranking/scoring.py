"""Gates and scores for one candidate, and the choice of a diverse top N.

Five parts, each 0..1, weighted into one total (defaults in ``RankingSettings``):

=========  ==========================================================================
fit        does it do the job, under the user's constraints (lexical coverage)
quality    evidence of quality: docs, releases, eval results in the card, usability
health     freshness of the code / model / data, and of its releases
trust      license, provenance, safe weight formats
adoption   real use, as a percentile among the candidates found, damped when inflated
=========  ==========================================================================

A part whose evidence is missing is ``None`` (unknown). Unknown counts as neutral (0.5) in the
total and is listed under ``unresolved``, so missing evidence neither helps nor hurts silently.
Gates are separate from scores: a failed gate removes a candidate however well it scores.

The thresholds and weights here are a first hypothesis to calibrate against an evaluation set
(docs/ranking-parameters.md, section 10), not tuned values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from statistics import fmean
from typing import Any

from open_intelligence.config import RankingSettings
from open_intelligence.ranking.adapters import has_safe_weights, hub_task, pickle_only
from open_intelligence.ranking.licenses import LICENSE_TRUST, classify
from open_intelligence.ranking.models import Candidate, Constraints, EvalResult
from open_intelligence.ranking.text import Vocabulary, coverage, jaccard

NEUTRAL = 0.5
PARTS = ("fit", "quality", "health", "trust", "adoption")
COVERED_AT = 0.6  # a must-have counts as covered from this coverage up


@dataclass
class Part:
    value: float | None
    reasons: list[str] = field(default_factory=list)

    @property
    def counted(self) -> float:
        """The value used in the total: unknown counts as neutral."""
        return NEUTRAL if self.value is None else self.value

    @property
    def percent(self) -> int | None:
        return None if self.value is None else round(self.value * 100)


@dataclass
class Scored:
    candidate: Candidate
    parts: dict[str, Part] = field(default_factory=dict)
    total: float = 0.0
    gate_failures: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    covered: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)


def _months_between(earlier: datetime, later: datetime) -> float:
    return max(0.0, (later - earlier).total_seconds() / 86400 / 30.44)


def ago(months: float) -> str:
    if months < 1:
        return "this month"
    if months < 24:
        return f"{round(months)} month{'s' if round(months) != 1 else ''} ago"
    return f"{months / 12:.0f} years ago"


def human(number: float) -> str:
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if number >= size:
            return f"{number / size:.1f}{suffix}"
    return f"{number:.0f}"


def _mix(pairs: list[tuple[float, float | None]]) -> float | None:
    """Weighted mean in which an unknown value counts as neutral; None when nothing at all is known.

    Leave a sub-part out of ``pairs`` when it does not apply; pass None when it applies but is unknown.
    """
    if not pairs or all(v is None for _, v in pairs):
        return None
    return sum(w * (NEUTRAL if v is None else v) for w, v in pairs) / sum(w for w, _ in pairs)


# --- fit ------------------------------------------------------------------------------------------

_LANGUAGE_ALIASES = {"js": "javascript", "ts": "typescript", "golang": "go", "cpp": "c++", "py": "python"}


def _lang(name: str) -> str:
    low = name.strip().lower()
    return _LANGUAGE_ALIASES.get(low, low)


def _constraint_factor(
    c: Candidate, constraints: Constraints, strong: Vocabulary, weak: Vocabulary
) -> tuple[float, list[str]]:
    factor, notes = 1.0, []
    wanted_lang = constraints.programming_language
    if wanted_lang and c.need in ("code", "notebook"):
        if c.programming_language is None:
            factor *= 0.85
        elif _lang(c.programming_language) == _lang(wanted_lang):
            notes.append(f"written in {c.programming_language}")
        elif _lang(wanted_lang) in strong or _lang(wanted_lang) in weak:
            factor *= 0.9
            notes.append(f"written in {c.programming_language}, with {wanted_lang} support mentioned")
        else:
            factor *= 0.5
            notes.append(f"written in {c.programming_language}, not {wanted_lang}")
    if constraints.libraries and c.need == "model":
        wanted = {w.lower() for w in constraints.libraries}
        have = {t.lower() for t in c.tags} | set(c.weight_formats or ()) | {(c.library or "").lower()}
        have |= {part.strip() for part in (c.library or "").lower().split(",")}
        if wanted & have:
            notes.append(f"works with {', '.join(sorted(wanted & have))}")
        elif c.library:
            factor *= 0.5
            notes.append(f"built for {c.library}, not {', '.join(sorted(wanted))}")
        else:
            factor *= 0.85
    if constraints.languages and c.need in ("model", "dataset"):
        wanted = {w.lower() for w in constraints.languages}
        have = {lang.lower() for lang in c.languages}
        if wanted & have:
            notes.append(f"covers {', '.join(sorted(wanted & have))}")
        elif "multilingual" in have:
            factor *= 0.9
            notes.append("multilingual (the specific language is not listed)")
        elif have:
            factor *= 0.4
            notes.append(f"covers {', '.join(sorted(have)[:4])}, not {', '.join(sorted(wanted))}")
        else:
            factor *= 0.8
            notes.append("language coverage is not stated")
    return factor, notes


GENERIC_ONLY = 0.75  # fit kept by a candidate that matches the task category but no specific phrasing


def fit_part(
    c: Candidate,
    phrases: list[str],
    must_have: bool,
    constraints: Constraints,
    specifics: list[str] | None = None,
) -> tuple[Part, list[str], list[str]]:
    """How well a candidate matches what was asked for.

    ``specifics`` are the search phrasings that are not just the name of a task category. Everything
    filed under "question answering" matches the words "question answering"; only some of it also
    matches "squad" or "reading comprehension". A candidate that matches the category alone keeps
    ``GENERIC_ONLY`` of its fit.
    """
    strong = Vocabulary(
        [
            c.id,
            c.title,
            c.description,
            " ".join(c.tags),
            c.task or "",
            c.library or "",
            c.programming_language or "",
            # A dataset's column names and label classes say what is in it ("tokens", "ner_tags").
            " ".join(c.structure.columns + c.structure.label_names) if c.structure else "",
        ]
    )
    weak = Vocabulary([c.doc_text])
    scores = [(p, coverage(p, strong, weak)) for p in phrases if p.strip()]
    covered: list[str] = []
    missing: list[str] = []
    reasons: list[str] = []
    specific = max((coverage(q, strong, weak) for q in specifics or [] if q.strip()), default=None)
    if not scores:
        core = NEUTRAL
    elif must_have:
        core = fmean(v for _, v in scores)
        covered = [p for p, v in scores if v >= COVERED_AT]
        missing = [p for p, v in scores if v < COVERED_AT]
        reasons.append(f"covers {len(covered)} of {len(scores)} must-haves")
        if specific is not None:
            core *= GENERIC_ONLY + (1 - GENERIC_ONLY) * specific
    else:
        best, core = max(scores, key=lambda pv: pv[1])
        if specific is not None and hub_task(best) is not None:
            # The best match is the category name itself: worth less than matching a specific phrasing.
            core = max(specific, GENERIC_ONLY * core)
        if core >= COVERED_AT:
            reasons.append(f"matches '{best}'")
    if specific is not None and specific < COVERED_AT and core > 0:
        reasons.append("matches the task category, but none of the specific search phrasings")
    factor, notes = _constraint_factor(c, constraints, strong, weak)
    return Part(core * factor, reasons + notes), covered, missing


# --- quality --------------------------------------------------------------------------------------


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) is not None


def _share(patterns: tuple[str, ...], text: str) -> float:
    return sum(_has(p, text) for p in patterns) / len(patterns)


_MODEL_CARD = (
    r"how to use|usage|quick ?start|example",
    r"intended use|\buses\b|use cases?",
    r"limitation|bias|risk|out[- ]of[- ]scope",
    r"training (data|procedure|details)|trained on|fine-?tuned on",
    r"evaluation|results|performance|benchmark",
)
_DATASET_CARD = (
    r"summary|description|overview|context|content",
    r"structure|fields|features|columns|schema|splits",
    r"source|collection|curation|creation|provenance|acknowledge",
    r"annotat|label",
    r"licen[sc]",
    r"limitation|bias|considerations",
)
_METRIC_IN_PROSE = r"\b(accuracy|f1|bleu|rouge|wer|cer|map|auc|recall|precision|perplexity)\b.{0,60}\d"


def _quality_code(c: Candidate, s: Scored) -> Part:
    if not c.doc_text:
        s.unresolved.append("README was not read")
        release = {True: 1.0, False: 0.3}.get(c.has_releases) if c.has_releases is not None else None
        return Part(release)
    doc = c.doc_text
    install = _has(
        r"pip install|npm i(nstall)? |yarn add|cargo (add|install)|go (get|install)|brew install|docker (run|pull)|installation",
        doc,
    )
    usage = "```" in doc or _has(r"usage|quick ?start|getting started|example", doc)
    tests = _has(r"actions/workflows|badge\.svg|travis-ci|circleci|codecov|coveralls|\btests?\b", doc)
    documented = 0.4 * min(1.0, len(doc) / 3000) + 0.3 * install + 0.3 * usage
    release = {True: 1.0, False: 0.3}.get(c.has_releases) if c.has_releases is not None else None
    reasons = []
    if install and usage:
        reasons.append("README has install and usage instructions")
    elif len(doc) < 600:
        reasons.append("README is very short")
    if c.has_releases is True:
        reasons.append("publishes releases")
    elif c.has_releases is False:
        reasons.append("no published releases")
    return Part(_mix([(0.5, documented), (0.3, release), (0.2, 1.0 if tests else 0.3)]), reasons)


def _quality_model(c: Candidate, s: Scored, standings: list[Standing]) -> Part:
    doc, reasons = c.doc_text, []
    cut_off = "card_cut_off" in c.flags  # the card's text was not reached: unknown, not empty
    evals: float | None
    if c.evals:
        verified = any(e.verified for e in c.evals)
        evals = 1.0 if verified else 0.7
        reasons.append(f"eval results in the card: {c.evals[0].describe()}")
        if standings:
            # Where other candidates report the same benchmark, the numbers are compared: the best of
            # the group gains, the worst loses. Without a shared benchmark nothing changes.
            evals = min(1.0, max(0.0, evals + 0.3 * (fmean(st.percentile for st in standings) - 0.5)))
            reasons.extend(st.describe() for st in standings[:2])
    elif (
        c.enriched
        and doc
        and (_has(r"^#+\s*(evaluation|results|performance|benchmark)", doc) or _has(_METRIC_IN_PROSE, doc))
    ):
        evals = 0.45
        reasons.append("results are described in the card text only (self-reported)")
    elif "model-index" in c.flags:
        evals = 0.6
    elif c.enriched and not cut_off:
        evals = 0.15
        reasons.append("no evaluation results in the card")
    else:
        evals = None
    card: float | None
    if c.enriched and doc:
        card = 0.5 * _share(_MODEL_CARD, doc) + 0.5 * min(1.0, len(doc) / 2500)
    elif c.enriched and not cut_off:
        card = 0.05
        reasons.append("model card is empty")
    else:
        card = None
        if not c.enriched:
            s.unresolved.append("model card was not read")
    return Part(_mix([(0.6, evals), (0.4, card)]), reasons)


_VIEWER_BUSY = "the Hub's dataset viewer did not answer (rate limit or outage)"
_TABULAR_FILES = frozenset({".csv", ".tsv", ".parquet", ".json", ".jsonl", ".sqlite", ".xlsx", ".feather"})


def _structure_score(c: Candidate, s: Scored, reasons: list[str]) -> float | None:
    """What the files themselves show: can it be loaded, is it split, are labels named, is the head clean."""
    structure = c.structure
    if c.source == "kaggle":
        if structure is None:
            return None
        kinds = ", ".join(structure.file_types) or "unknown"
        columns = f"{structure.column_count} columns in " if structure.column_count else ""
        reasons.append(f"{columns}{kinds} files")
        s.unresolved.append(
            "row counts and sample rows are not available from Kaggle, so rows were not checked"
        )
        tabular = bool(set(structure.file_types) & _TABULAR_FILES)
        return (0.65 if tabular else 0.45) + (0.05 if structure.column_count else 0.0)
    if "loading_script" in c.flags:
        s.warnings.append(
            "loads through a Python script in the repository: the Hub's viewer cannot read it, and current "
            "versions of the datasets library do not run such scripts"
        )
        return 0.15
    if "viewer_failed" in c.flags:
        s.warnings.append("the Hub's viewer cannot load its rows: loading it yourself may fail too")
        return 0.3
    if structure is None:
        if "viewer_unavailable" in c.flags:
            s.unresolved.append(_VIEWER_BUSY + ", so structure and rows were not checked")
            return None
        if "no_viewer" in c.flags:
            s.unresolved.append(
                "the Hub's viewer could not read its files, so structure and rows were not checked"
            )
            return 0.35
        return None
    score = 0.6
    if structure.splits:
        shown = ", ".join(
            f"{name.rsplit('/', 1)[-1]} {human(rows) if rows is not None else '(rows unknown)'}"
            for name, rows in list(structure.splits.items())[:4]
        )
        reasons.append(f"splits: {shown}")
    if structure.has_held_out_split:
        score += 0.15
    elif structure.splits:
        reasons.append("no test or validation split: hold some data out yourself")
    if structure.label_names:
        score += 0.1
        reasons.append(f"label classes are named ({', '.join(structure.label_names[:6])})")
    if structure.total_rows is not None and structure.total_rows < 200:
        score -= 0.2
        s.warnings.append(f"very small: {structure.total_rows:.0f} rows")
    if structure.sample_rows:
        rows = structure.sample_rows
        if structure.sample_duplicate_rows:
            score -= 0.25
            s.warnings.append(f"{structure.sample_duplicate_rows} repeated rows among the first {rows}")
        elif structure.sample_empty_cells:
            score -= 0.05
            s.warnings.append(
                f"{structure.sample_empty_cells} empty cells in the first {rows} rows: it has missing values"
            )
        else:
            score += 0.15
            reasons.append(f"no empty cells or repeated rows in the first {rows} rows")
    elif "viewer_unavailable" in c.flags:
        s.unresolved.append(_VIEWER_BUSY + ", so sample rows were not checked")
    else:
        s.unresolved.append("sample rows were not read")
    return min(1.0, max(0.0, score))


def _quality_dataset(c: Candidate, s: Scored) -> Part:
    doc, reasons = c.doc_text, []
    card = (0.6 * _share(_DATASET_CARD, doc) + 0.4 * min(1.0, len(doc) / 2500)) if doc else None
    if c.source == "kaggle":
        usability = c.metrics.get("usability")
        if usability is not None:
            reasons.append(
                f"Kaggle usability {usability:.2f} (how complete the documentation is, not data quality)"
            )
        if c.enriched and not doc:
            card = 0.05
        structure = _structure_score(c, s, reasons)
        return Part(_mix([(0.45, usability), (0.25, card), (0.3, structure)]), reasons)
    tags = " ".join(c.tags).lower()
    described = [
        k in tags for k in ("task_categories:", "language:", "size_categories:", "license:", "format:")
    ]
    meta = sum(described) / len(described) if c.tags else None
    if c.enriched and not doc and "card_cut_off" not in c.flags:
        card = 0.05
        reasons.append("dataset card is empty")
    elif not c.enriched:
        s.unresolved.append("dataset card was not read")
    structure = _structure_score(c, s, reasons)
    return Part(_mix([(0.4, card), (0.2, meta), (0.4, structure)]), reasons)


def quality_part(c: Candidate, s: Scored, standings: list[Standing] | None = None) -> Part:
    if c.need == "code":
        return _quality_code(c, s)
    if c.need == "model":
        return _quality_model(c, s, standings or [])
    if c.need == "dataset":
        return _quality_dataset(c, s)
    return Part(None)


# --- benchmark comparison -------------------------------------------------------------------------

_LOWER_IS_BETTER = re.compile(r"loss|wer|cer|error|perplexity|ppl|mae|mse|rmse|\bter\b")
_BOUNDED = re.compile(
    r"accuracy|acc\b|f1|precision|recall|auc|exact|\bem\b|map|mrr|ndcg|bleu|rouge|wer|cer|meteor|chrf"
)


@dataclass
class Standing:
    """One candidate's place among the candidates that report the same benchmark."""

    benchmark: str
    value: float
    rank: int  # 1 = best
    of: int
    percentile: float  # 1 = best
    verified: bool

    def describe(self) -> str:
        if self.rank == 1:
            place = "best"
        elif self.rank == self.of:
            place = "last"
        else:
            place = {2: "2nd", 3: "3rd"}.get(self.rank, f"{self.rank}th")
        return f"{self.benchmark}: {self.value:.4g}, {place} of the {self.of} candidates that report it"


def _comparable(metric: str, value: float) -> float:
    """Cards report bounded metrics as 0.91 or as 91: put both on one scale."""
    return value / 100 if _BOUNDED.search(metric) and value > 1.5 else value


def benchmark_groups(
    candidates: list[Candidate],
) -> dict[tuple[str, str, str, str], list[tuple[Candidate, EvalResult]]]:
    """Eval results grouped by benchmark, for benchmarks at least two candidates report."""
    groups: dict[tuple[str, str, str, str], list[tuple[Candidate, EvalResult]]] = {}
    for c in candidates:
        seen = set()
        for result in c.evals:
            if result.value is None or result.benchmark in seen:
                continue
            seen.add(result.benchmark)
            groups.setdefault(result.benchmark, []).append((c, result))
    return {key: entries for key, entries in groups.items() if len(entries) >= 2}


def benchmark_standings(candidates: list[Candidate]) -> dict[str, list[Standing]]:
    """For each candidate, where its numbers stand on every benchmark it shares with another candidate.

    Only identical benchmarks are compared (same dataset, config, split and metric). The numbers
    are mostly self-reported: a comparison says who claims more, and ``verified`` says who was checked.
    """
    standings: dict[str, list[Standing]] = {}
    for (_, _, _, metric), entries in benchmark_groups(candidates).items():
        sign = -1.0 if _LOWER_IS_BETTER.search(metric) else 1.0
        oriented = [sign * _comparable(metric, r.value or 0.0) for _, r in entries]
        for (c, result), value in zip(entries, oriented, strict=True):
            standings.setdefault(c.key, []).append(
                Standing(
                    benchmark=result.benchmark_name,
                    value=result.value or 0.0,
                    rank=1 + sum(other > value for other in oriented),
                    of=len(entries),
                    percentile=_percentile(value, oriented),
                    verified=result.verified,
                )
            )
    return standings


def benchmark_tables(candidates: list[Candidate], limit: int = 5) -> list[dict[str, Any]]:
    """The comparisons themselves, best first, for the result: the reader should see the numbers."""
    tables = []
    for (_, _, _, metric), entries in benchmark_groups(candidates).items():
        lower = bool(_LOWER_IS_BETTER.search(metric))
        ordered = sorted(entries, key=lambda e: _comparable(metric, e[1].value or 0.0), reverse=not lower)
        tables.append(
            {
                "benchmark": ordered[0][1].benchmark_name,
                "better": "lower" if lower else "higher",
                "results": [{"id": c.id, "value": r.value, "verified": r.verified} for c, r in ordered],
            }
        )
    tables.sort(key=lambda t: -len(t["results"]))
    return tables[:limit]


# --- health ---------------------------------------------------------------------------------------


def health_part(c: Candidate, s: Scored, now: datetime, settings: RankingSettings) -> Part:
    if c.updated_at is None:
        s.unresolved.append("last update date is not known")
        return Part(None)
    months = _months_between(c.updated_at, now)
    fresh = 0.5 ** (months / settings.half_life_months[c.need])
    reasons = [f"updated {ago(months)}"]
    if c.need != "code":
        return Part(fresh, reasons)
    release: float | None = None
    if c.released_at is not None:
        release_months = _months_between(c.released_at, now)
        release = 0.5 ** (release_months / settings.half_life_months["code"])
        reasons.append(f"latest release {ago(release_months)}")
    elif c.has_releases is False:
        release = 0.3
    return Part(_mix([(0.6, fresh), (0.4, release)]), reasons)


# --- trust ----------------------------------------------------------------------------------------


def trust_part(c: Candidate, s: Scored) -> Part:
    reasons = []
    kind = classify(c.license)
    license_score: float | None
    if c.license or c.license_reported:
        license_score = LICENSE_TRUST[kind]
        reasons.append(f"license: {c.license or 'none declared'} ({kind})")
        if kind == "unclear":
            s.unresolved.append(
                f"license is unclear ({c.license or 'none declared'}): confirm the terms before use"
            )
        elif kind == "use-restricted":
            s.warnings.append(f"license {c.license} restricts some uses: read its terms")
        elif kind == "non-commercial":
            s.warnings.append(f"license {c.license} does not allow commercial use")
    else:
        license_score = None
        s.unresolved.append("license was not reported by the search and has not been checked")

    pairs: list[tuple[float, float | None]] = [(0.5, license_score)]
    if c.need == "code":
        pairs.append((0.2, 0.3 if "fork" in c.flags else 1.0))
        if "fork" in c.flags:
            reasons.append("is a fork of another repository")
    elif c.need == "model":
        if c.lineage in ("quantized", "adapter", "merge") and c.base_model:
            pairs.append((0.2, 0.8))
            reasons.append(f"a {c.lineage} version of {c.base_model}, not the original")
        elif c.source == "kaggle":
            pairs.append((0.2, 1.0 if "provenance_stated" in c.flags else 0.6))
        else:
            pairs.append((0.2, 1.0))
    elif "original_source" in c.flags:
        pairs.append((0.2, 1.0))

    safety: float | None = None
    if c.need == "model":
        only_pickle, safe = pickle_only(c), has_safe_weights(c)
        hint = " (from tags; files not listed)" if "weights_from_tags" in c.flags else ""
        if only_pickle:
            safety = 0.2
            s.warnings.append(f"ships only pickle weights, which can run code when loaded{hint}")
        elif safe:
            safety = 1.0
            safe_formats = sorted(f for f in c.weight_formats or () if f in ("safetensors", "gguf", "onnx"))
            reasons.append(f"safe weight formats available ({', '.join(safe_formats)})")
        elif c.weight_formats:
            safety = 0.6
            s.warnings.append(f"no safetensors, GGUF or ONNX weights{hint}")
        else:
            s.unresolved.append("weight file formats are not known")
        if "custom_code" in c.flags:
            safety = min(safety if safety is not None else 1.0, 0.5)
            s.warnings.append("needs trust_remote_code: loading it runs code from the repository")
        pairs.append((0.3, safety))
    elif c.need == "code":
        pairs.append((0.3, _security_score(c, s, reasons)))
    if "access_restricted" in c.flags:
        s.warnings.append("its files could not be read without access: it is gated or private")
    return Part(_mix(pairs), reasons)


def _security_score(c: Candidate, s: Scored, reasons: list[str]) -> float | None:
    """OpenSSF Scorecard and known vulnerabilities in the release a user would install today."""
    evidence = c.security
    if evidence is None:
        s.unresolved.append("OpenSSF Scorecard and known vulnerabilities were not looked up")
        return None
    scorecard: float | None = None
    if evidence.scorecard is not None:
        scorecard = evidence.scorecard / 10
        when = f", scanned {evidence.scorecard_date}" if evidence.scorecard_date else ""
        reasons.append(f"OpenSSF Scorecard {evidence.scorecard:.1f}/10{when}")
        checks = evidence.scorecard_checks
        if checks.get("Maintained") == 0:
            s.warnings.append("Scorecard: no maintenance activity in the last 90 days")
        if checks.get("Dangerous-Workflow") == 0:
            s.warnings.append("Scorecard: a dangerous pattern in its CI workflows")
        if 0 <= checks.get("Vulnerabilities", 10) <= 3:
            s.warnings.append("Scorecard: open known vulnerabilities in the repository or its dependencies")
    advisories: float | None = None
    release = (
        f"{evidence.package} {evidence.package_version}".strip() if evidence.package else "the latest release"
    )
    if evidence.advisories is None:
        pass
    elif not evidence.advisories:
        advisories = 1.0
        reasons.append(f"no known vulnerabilities affect {release}")
    else:
        worst = max((a.cvss or 0.0) for a in evidence.advisories)
        advisories = 0.2 if worst >= 7 or any(a.cvss is None for a in evidence.advisories) else 0.5
        listed = ", ".join(f"{a.id} ({a.severity})" for a in evidence.advisories[:3])
        s.warnings.append(f"{len(evidence.advisories)} known vulnerabilities affect {release}: {listed}")
    s.unresolved.extend(note for note in evidence.notes if note not in s.unresolved)
    return _mix([(0.5, advisories), (0.5, scorecard)])


# --- adoption -------------------------------------------------------------------------------------

# Which counts mean adoption on each platform, and how much each weighs.
ADOPTION_METRICS: dict[tuple[str, str], tuple[tuple[str, float, str], ...]] = {
    ("github", "code"): (("stars", 0.6, "stars"), ("forks", 0.4, "forks")),
    ("huggingface", "model"): (("downloads", 0.6, "downloads in 30 days"), ("likes", 0.4, "likes")),
    ("huggingface", "dataset"): (("downloads", 0.6, "downloads in 30 days"), ("likes", 0.4, "likes")),
    ("kaggle", "dataset"): (
        ("notebooks", 0.5, "notebooks built on it"),
        ("downloads", 0.3, "downloads"),
        ("votes", 0.2, "votes"),
    ),
    ("kaggle", "model"): (("votes", 1.0, "votes"),),
    ("kaggle", "notebook"): (("votes", 1.0, "votes"),),
}


def inflation(c: Candidate) -> str | None:
    """A reason to distrust the popularity numbers, when independent signals disagree."""
    m = c.metrics
    if c.source == "github":
        stars, forks = m.get("stars", 0), m.get("forks")
        if stars >= 500 and forks is not None and forks / stars < 0.01:
            return f"{human(stars)} stars but only {human(forks)} forks: the stars may be inflated"
    elif c.source == "huggingface":
        downloads, likes = m.get("downloads", 0), m.get("likes")
        if downloads >= 50_000 and likes is not None and likes / downloads < 2e-5:
            return f"{human(downloads)} downloads but only {human(likes)} likes: the downloads look automated"
    elif c.source == "kaggle" and c.need == "dataset":
        downloads = m.get("downloads", 0)
        engaged = m.get("votes", 0) + m.get("notebooks", 0)
        if downloads >= 2000 and engaged / downloads < 5e-4:
            return (
                f"{human(downloads)} downloads but almost no votes or notebooks: the downloads look automated"
            )
    return None


def _percentile(value: float, values: list[float]) -> float:
    below = sum(v < value for v in values)
    equal = sum(v == value for v in values)
    return (below + 0.5 * equal) / len(values)


def adoption_parts(group: list[Candidate]) -> dict[str, tuple[Part, str | None]]:
    """Adoption of each candidate relative to the others found on the same source for the same need."""
    if not group:
        return {}
    metrics = ADOPTION_METRICS.get((group[0].source, group[0].need), ())
    columns = {name: [c.metrics[name] for c in group if name in c.metrics] for name, _, _ in metrics}
    out: dict[str, tuple[Part, str | None]] = {}
    for c in group:
        pairs, shown = [], []
        for name, weight, label in metrics:
            if name in c.metrics and columns[name]:
                pairs.append((weight, _percentile(c.metrics[name], columns[name])))
                shown.append(f"{human(c.metrics[name])} {label}")
        value = _mix(list(pairs))
        reasons = []
        if value is not None:
            standing = (
                "more than most" if value >= 0.66 else "fewer than most" if value <= 0.33 else "typical"
            )
            reasons.append(f"{', '.join(shown)}: {standing} of the {len(group)} candidates on {c.source}")
        warning = inflation(c)
        if warning and value is not None:
            value *= 0.5
        out[c.key] = (Part(value, reasons), warning)
    return out


# --- gates ----------------------------------------------------------------------------------------


def gate_failures(
    c: Candidate,
    constraints: Constraints,
    now: datetime,
    fit: float | None,
    settings: RankingSettings,
    final: bool,
) -> list[tuple[str, str]]:
    failures = []
    if "archived" in c.flags or "disabled" in c.flags:
        failures.append(("archived", "archived by its owner: no longer maintained"))
    if "private" in c.flags:
        failures.append(("private", "not public"))
    critical = [a for a in (c.security.advisories or []) if (a.cvss or 0) >= 9] if c.security else []
    if critical:
        failures.append(
            ("security", f"a critical known vulnerability affects its latest release: {critical[0].id}")
        )
    kind = classify(c.license)
    if constraints.commercial_use and kind == "non-commercial":
        failures.append(("license", f"license {c.license} does not allow commercial use"))
    if constraints.copyleft_ok is False and kind == "copyleft":
        failures.append(("license", f"copyleft license {c.license} is not acceptable for this project"))
    if constraints.allow_gated is False and "access_restricted" in c.flags:
        failures.append(("gated", "needs an access request (gated)"))
    if (
        c.need == "model"
        and pickle_only(c)
        and "weights_from_tags" not in c.flags
        and not constraints.allow_pickle_weights
    ):
        failures.append(
            (
                "weights",
                "ships only pickle weights, which can run code when loaded (allow_pickle_weights includes it)",
            )
        )
    limit = constraints.max_model_size_gb
    if c.need == "model" and limit and c.size_bytes and c.size_bytes > limit * 1e9:
        failures.append(("size", f"weights are {c.size_bytes / 1e9:.1f} GB, over the {limit:g} GB limit"))
    if constraints.max_age_months and c.updated_at is not None:
        months = _months_between(c.updated_at, now)
        if months > constraints.max_age_months:
            failures.append(
                ("age", f"last updated {ago(months)}, over the {constraints.max_age_months}-month limit")
            )
    if final and fit is not None and fit < settings.min_fit:
        failures.append(("fit", f"does not match the need closely enough (fit {fit:.2f})"))
    return failures


# --- total ----------------------------------------------------------------------------------------


def score_group(
    candidates: list[Candidate],
    phrases: list[str],
    must_have: bool,
    constraints: Constraints,
    settings: RankingSettings,
    now: datetime,
    final: bool,
    specifics: list[str] | None = None,
) -> list[Scored]:
    """Score candidates for one need (possibly from several sources), best first."""
    adoption: dict[str, tuple[Part, str | None]] = {}
    for source in {c.source for c in candidates}:
        adoption.update(adoption_parts([c for c in candidates if c.source == source]))
    standings = benchmark_standings([c for c in candidates if c.need == "model"])
    scored = []
    for c in candidates:
        s = Scored(candidate=c)
        s.parts["fit"], s.covered, s.missing = fit_part(c, phrases, must_have, constraints, specifics)
        s.parts["quality"] = quality_part(c, s, standings.get(c.key))
        s.parts["health"] = health_part(c, s, now, settings)
        s.parts["trust"] = trust_part(c, s)
        s.parts["adoption"], inflated = adoption.get(c.key, (Part(None), None))
        if inflated:
            s.warnings.append(inflated)
        if c.need == "model" and constraints.max_model_size_gb and not c.size_bytes:
            s.unresolved.append("weights size is not known, so the size limit was not checked")
        s.unresolved.extend(note for note in c.read_notes if note not in s.unresolved)
        s.gate_failures = gate_failures(c, constraints, now, s.parts["fit"].value, settings, final)
        if (
            final
            and must_have
            and phrases
            and not s.covered
            and not any(g == "fit" for g, _ in s.gate_failures)
        ):
            s.gate_failures.append(("fit", "covers none of the must-haves"))
        s.total = sum(settings.weights[name] * s.parts[name].counted for name in PARTS)
        scored.append(s)
    scored.sort(key=lambda s: (-s.total, s.candidate.position, s.candidate.id))
    return scored


# --- a diverse top N ------------------------------------------------------------------------------


def similarity(a: Candidate, b: Candidate) -> float:
    """1 = the same thing in another wrapper; 0 = unrelated options."""
    if a.need == "model":
        family_a = a.base_model if a.lineage in ("quantized", "adapter") else None
        family_b = b.base_model if b.lineage in ("quantized", "adapter") else None
        if (family_a and family_a in (b.id, family_b)) or (family_b and family_b == a.id):
            return 1.0  # a conversion or adapter of the other, or of the same model
        if a.base_model and a.base_model == b.base_model:
            return 0.5  # two fine-tunes of one base model
    names = jaccard(a.title or a.name, b.title or b.name)
    if names >= 0.8:
        return 0.9  # a mirror or re-upload under another owner
    same_owner = 0.3 if a.source == b.source and a.owner and a.owner == b.owner else 0.0
    return max(0.5 * names, same_owner)


def pick_diverse(ranked: list[Scored], top: int, penalty: float = 0.1) -> list[Scored]:
    """Greedy: best first, near-duplicates of a pick become its ``variants`` instead of a pick."""
    picks: list[Scored] = []
    pool = list(ranked)
    while pool and len(picks) < top:

        def worth(s: Scored) -> float:
            closest = max((similarity(s.candidate, p.candidate) for p in picks), default=0.0)
            return s.total - penalty * closest

        best = max(pool, key=worth)
        pool.remove(best)
        twin = next((p for p in picks if similarity(best.candidate, p.candidate) >= 0.9), None)
        if twin is not None:
            twin.variants.append(best.candidate.id)
        else:
            picks.append(best)
    return picks
