"""The ranking pipeline: route, gather, gate, score, read the shortlist, re-score, pick.

1. Route the request to sources (router).
2. Gather candidates: several phrasings per need, the platform's default order plus one
   popularity sort, merged and de-duplicated.
3. Score every candidate from search metadata and apply the metadata gates.
4. Read the best few per need and source (README, model card, dataset card, file list).
5. Score again with what was read, apply the remaining gates.
6. Pick a diverse top N per need and explain each pick: what put it there, what is uncertain.

Scores are computed per need (a repository is never compared with a dataset). Adoption is a
percentile among the candidates found on the same source, so sources with different scales can
share one list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import anyio

from open_intelligence.config import RankingSettings, Settings
from open_intelligence.connector import CallOutcome
from open_intelligence.ranking.adapters import ADAPTERS, Adapter, hub_task
from open_intelligence.ranking.evidence import SecurityLookup
from open_intelligence.ranking.licenses import classify
from open_intelligence.ranking.models import Call, Candidate, Constraints
from open_intelligence.ranking.scoring import (
    PARTS,
    Scored,
    benchmark_tables,
    human,
    pick_diverse,
    score_group,
)
from open_intelligence.router import route
from open_intelligence.sources import NEEDS, Need

CONCURRENCY = 4  # upstream calls in flight per source


@dataclass
class RankRequest:
    task: str
    query: str = ""
    needs: list[str] | None = None
    queries: dict[str, list[str]] = field(default_factory=dict)
    must_have: dict[str, list[str]] = field(default_factory=dict)
    constraints: Constraints = field(default_factory=lambda: Constraints())
    top: int = 3


@dataclass
class _SourceStatus:
    source: str
    ok: bool = False
    found: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source, "ok": self.ok, "found": self.found}
        if self.errors:
            out["errors"] = list(dict.fromkeys(self.errors))[:3]
        return out


Planned = tuple[Adapter, Call]


class Upstream(Protocol):
    """What the pipeline needs from a connector: its settings, and a way to call upstream tools.

    ``Connector`` is the real one; the evaluation's ``Recorder`` is another, which replays saved answers.
    """

    settings: Settings

    async def call_many(
        self,
        source_id: str,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        full_text: bool = False,
        concurrency: int = 1,
    ) -> list[CallOutcome]: ...


class Ranker:
    def __init__(
        self,
        connector: Upstream,
        settings: RankingSettings | None = None,
        now: Callable[[], datetime] | None = None,
        security: SecurityLookup | None = None,
    ) -> None:
        self.connector = connector
        self.settings = settings or connector.settings.ranking
        self._now = now or (lambda: datetime.now(UTC))
        self.security = security or SecurityLookup(connector.settings.evidence)

    async def find(self, request: RankRequest) -> dict[str, Any]:
        settings, now = self.settings, self._now()
        top = max(1, min(request.top, 5))
        decision = route(request.task, request.needs)

        # 1-2. Plan and run the searches.
        plan: dict[str, list[Planned]] = {}
        for source_plan in decision.sources:
            for need in source_plan.needs:
                adapter = ADAPTERS.get((source_plan.source, need))
                queries = self._queries(request, need)
                if adapter is None or not queries:
                    continue
                calls = adapter.search_calls(queries, settings.per_query, request.constraints.languages)
                plan.setdefault(source_plan.source, []).extend((adapter, call) for call in calls)
        outcomes = await self._run(plan)

        pools: dict[Need, dict[str, Candidate]] = {}
        status: dict[tuple[str, Need], _SourceStatus] = {}
        platform_top: dict[tuple[str, Need], list[str]] = {}
        for source, planned in plan.items():
            for (adapter, call), outcome in zip(planned, outcomes[source], strict=True):
                state = status.setdefault((source, adapter.need), _SourceStatus(source))
                if not outcome.ok:
                    state.errors.append(outcome.error or "search failed")
                    continue
                state.ok = True
                found = adapter.parse_search(outcome, settings.per_query)
                if not found and len(outcome.text) > 400:
                    state.errors.append(
                        "results could not be read: the source's output format may have changed"
                    )
                if call.baseline:
                    platform_top[(source, adapter.need)] = [c.id for c in found[:top]]
                pool = pools.setdefault(adapter.need, {})
                for position, candidate in enumerate(found):
                    kept = pool.setdefault(candidate.key, candidate)
                    if call.label and call.label not in kept.found_by:
                        kept.found_by.append(call.label)
                    if call.baseline:
                        kept.position = min(kept.position, position)
                state.found = sum(1 for c in pool.values() if c.source == source)

        # 3-4. Score from metadata, then read the shortlist of each source. When the gates that
        # need a read (pickle-only weights, minimum fit) knock out most of a shortlist, read one
        # more batch, so a source is not left with fewer survivors than picks to fill.
        eligible: set[str] = set()
        for read_round in range(2):
            reading: dict[str, list[Planned]] = {}
            batches: list[tuple[str, Adapter, list[Candidate]]] = []
            for need, pool in pools.items():
                phrases, is_must = self._phrases(request, need)
                scored = score_group(
                    list(pool.values()),
                    phrases,
                    is_must,
                    request.constraints,
                    settings,
                    now,
                    final=read_round > 0,
                    specifics=self._specifics(request, need),
                )
                for source in sorted({c.source for c in pool.values()}):
                    adapter = ADAPTERS[(source, need)]
                    passing = [
                        s.candidate for s in scored if s.candidate.source == source and not s.gate_failures
                    ]
                    if not adapter.reads_shortlist:
                        eligible.update(c.key for c in passing)
                        continue
                    unread = [c for c in passing if c.key not in eligible]
                    if read_round > 0 and len(passing) - len(unread) >= top + 2:
                        continue
                    batch = unread[: settings.read_per_source]
                    eligible.update(c.key for c in batch)
                    batches.append((source, adapter, batch))
                    reading.setdefault(source, []).extend(
                        (adapter, call) for call in adapter.enrich_calls(batch)
                    )
            if not reading:
                break
            await self._read(reading, pools)
            # What can only be asked once the first answers are in (a dataset's sample rows need
            # its config and split), and security evidence for the repositories just read.
            follow_up: dict[str, list[Planned]] = {}
            for source, adapter, batch in batches:
                follow_up.setdefault(source, []).extend(
                    (adapter, call) for call in adapter.follow_up_calls(batch)
                )
            await self._read(follow_up, pools)
            await self.security.lookup_many(
                [c for _, adapter, batch in batches if adapter.need == "code" for c in batch]
            )

        # 5-6. Final scores, gates and picks, per need.
        results = []
        all_picks: list[Scored] = []
        contenders: list[Scored] = []
        for need in NEEDS:
            if need not in decision.need_names:
                continue
            sources = [s for (_, n), s in status.items() if n == need]
            entry: dict[str, Any] = {"need": need}
            pool = pools.get(need, {})
            phrases, is_must = self._phrases(request, need)
            scored = score_group(
                list(pool.values()),
                phrases,
                is_must,
                request.constraints,
                settings,
                now,
                final=True,
                specifics=self._specifics(request, need),
            )
            in_running = [s for s in scored if s.candidate.key in eligible and not s.gate_failures]
            picks = pick_diverse(in_running, top)
            all_picks.extend(picks)
            contenders.extend(scored[:10])
            if not sources:
                entry["verdict"] = "not_searched"
                entry["note"] = (
                    "no source was searched for this need (excluded by the request, or no query given)"
                )
            elif not any(s.ok for s in sources):
                entry["verdict"] = "inconclusive"
                entry["note"] = (
                    "no source could be searched, so nothing can be concluded: fix the errors and retry"
                )
            elif not pool:
                entry["verdict"] = "no_candidates"
                entry["note"] = (
                    "the searches returned nothing: try other keywords before concluding nothing exists"
                )
            elif not picks:
                entry["verdict"] = "no_fit"
                entry["note"] = "candidates were found but none passed the gates: see rejected"
            else:
                entry["verdict"] = "picks"
                if any(not s.ok for s in sources):
                    entry["note"] = (
                        "some sources could not be searched: these picks may not be the full picture"
                    )
            entry["picks"] = [_pick(rank, s) for rank, s in enumerate(picks, start=1)]
            chosen = {s.candidate.key for s in picks} | {v for s in picks for v in s.variants}
            entry["runners_up"] = [
                {"id": s.candidate.id, "source": s.candidate.source, "score": round(s.total * 100)}
                for s in in_running
                if s.candidate.key not in chosen and s.candidate.id not in chosen
            ][:5]
            if need == "model":
                # Numbers from model cards, side by side, wherever two candidates report the same benchmark.
                tables = benchmark_tables([s.candidate for s in in_running])
                if tables:
                    entry["benchmark_comparison"] = tables
            entry["considered"] = len(pool)
            entry["read_in_depth"] = sum(1 for c in pool.values() if c.enriched)
            entry["rejected"] = _rejected(scored)
            entry["platform_top"] = {
                source: ids for (source, n), ids in platform_top.items() if n == need and ids
            }
            entry["sources"] = [s.as_dict() for s in sources]
            results.append(entry)

        return {
            "routing": decision.as_dict(),
            "results": results,
            "constraints": {
                "given": request.constraints.given(),
                "assumed": _assumptions(request.constraints, decision.need_names),
            },
            "questions_for_user": _questions(request.constraints, all_picks, contenders),
            "method": (
                "Gates first (archived, a critical known vulnerability, license vs constraints, pickle-only weights, "
                "size, age, minimum fit), then a score per need: "
                f"{', '.join(f'{name} {settings.weights[name]:.0%}' for name in PARTS)}. Fit is lexical (word "
                "matching), adoption is a percentile among the candidates found. Unknown evidence counts as "
                "neutral and is listed under unresolved_checks. The weights are a first hypothesis: see "
                "docs/evaluation.md for how they score against the seed evaluation set."
            ),
        }

    # --- helpers --------------------------------------------------------------------------------

    def _queries(self, request: RankRequest, need: str) -> list[str]:
        given = request.queries.get(need) or [request.query]
        cleaned = [q.strip() for q in given if isinstance(q, str) and q.strip()]
        return list(dict.fromkeys(cleaned))[: self.settings.max_queries]

    def _phrases(self, request: RankRequest, need: str) -> tuple[list[str], bool]:
        """What fit is measured against: the must-haves when given, otherwise the search phrasings."""
        must = [m.strip() for m in request.must_have.get(need, []) if isinstance(m, str) and m.strip()]
        if must:
            return must, True
        return self._queries(request, need), False

    def _specifics(self, request: RankRequest, need: str) -> list[str] | None:
        """The search phrasings that say more than the name of a task category. None when there are
        none, or when every phrasing is specific (then there is nothing to tell apart)."""
        queries = self._queries(request, need)
        specific = [q for q in queries if hub_task(q) is None]
        return specific if specific and len(specific) < len(queries) else None

    async def _run(self, plan: dict[str, list[Planned]]) -> dict[str, list[CallOutcome]]:
        outcomes: dict[str, list[CallOutcome]] = {}

        async def one(source: str, planned: list[Planned]) -> None:
            calls = [(call.tool, call.arguments) for _, call in planned]
            outcomes[source] = await self.connector.call_many(
                source, calls, full_text=True, concurrency=CONCURRENCY
            )

        async with anyio.create_task_group() as tg:
            for source, planned in plan.items():
                if planned:
                    tg.start_soon(one, source, planned)
        return {source: outcomes.get(source, []) for source in plan}

    async def _read(self, plan: dict[str, list[Planned]], pools: dict[Need, dict[str, Candidate]]) -> None:
        """Run enrichment calls and let each adapter write what it learned onto its candidates."""
        plan = {source: planned for source, planned in plan.items() if planned}
        if not plan:
            return
        outcomes = await self._run(plan)
        for source, planned in plan.items():
            for (adapter, call), outcome in zip(planned, outcomes[source], strict=True):
                by_id = {c.id: c for c in pools[adapter.need].values() if c.source == source}
                adapter.apply(by_id, call, outcome)


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def _pick(rank: int, s: Scored) -> dict[str, Any]:
    c = s.candidate
    declared = c.license or ("none declared" if c.license_reported else None)
    facts: dict[str, Any] = {
        "license": declared,
        "license_kind": classify(c.license) if declared else None,
        "updated": c.updated_at.date().isoformat() if c.updated_at else None,
        "latest_release": c.released_at.date().isoformat() if c.released_at else None,
        "programming_language": c.programming_language,
        "task": c.task,
        "library": c.library,
        "languages": c.languages[:8],
        "based_on": c.base_model,
        "parameters": human(c.params) if c.params else None,
        "size": f"{c.size_bytes / 1e9:.2f} GB" if c.size_bytes and c.size_bytes >= 1e7 else None,
        "weight_formats": sorted(c.weight_formats) if c.weight_formats else None,
        "eval_results": [e.describe() for e in c.evals[:4]],
        "metrics": {k: (round(v, 2) if v < 10 else int(v)) for k, v in sorted(c.metrics.items())},
    }
    if c.structure is not None:
        facts["rows"] = human(c.structure.total_rows) if c.structure.total_rows else None
        facts["splits"] = {
            name: human(rows) if rows is not None else "unknown"
            for name, rows in list(c.structure.splits.items())[:6]
        }
        facts["columns"] = c.structure.columns[:12] or c.structure.column_count
        facts["label_classes"] = c.structure.label_names[:12]
        facts["file_types"] = c.structure.file_types
    if c.security is not None:
        facts["openssf_scorecard"] = c.security.scorecard
        facts["scorecard_scanned"] = c.security.scorecard_date
        if c.security.package:
            facts["package_checked"] = f"{c.security.package} {c.security.package_version}"
        if c.security.advisories is not None:
            facts["known_vulnerabilities"] = [
                {"id": a.id, "severity": a.severity, "title": a.title} for a in c.security.advisories
            ] or "none in the current release"
    out: dict[str, Any] = {
        "rank": rank,
        "id": c.id,
        "source": c.source,
        "url": c.url,
        "score": round(s.total * 100),
        "scores": {name: s.parts[name].percent for name in PARTS},
        "why": _unique([reason for name in PARTS for reason in s.parts[name].reasons]),
    }
    if s.covered or s.missing:
        out["covers"] = s.covered
        out["missing"] = s.missing
    out["facts"] = {k: v for k, v in facts.items() if v not in (None, [], {}, "")}
    out["warnings"] = _unique(s.warnings)
    out["unresolved_checks"] = _unique(s.unresolved)
    if s.variants:
        out["also_available_as"] = s.variants
    if c.position < 10_000:
        out["platform_position"] = c.position + 1
    if c.description:
        out["untrusted_summary"] = c.description[:240]
    return out


# Rejections the user can change (their constraints) first, "did not match" last.
_GATE_ORDER = {
    "license": 0,
    "size": 0,
    "age": 0,
    "gated": 0,
    "weights": 1,
    "archived": 1,
    "private": 1,
    "fit": 2,
}


def _rejected(scored: list[Scored], limit: int = 5) -> list[dict[str, str]]:
    failed = [s for s in scored if s.gate_failures]
    failed.sort(key=lambda s: (_GATE_ORDER.get(s.gate_failures[0][0], 1), -s.total))
    return [
        {
            "id": s.candidate.id,
            "source": s.candidate.source,
            "gate": s.gate_failures[0][0],
            "reason": s.gate_failures[0][1],
        }
        for s in failed[:limit]
    ]


def _assumptions(constraints: Constraints, needs: list[Need]) -> list[str]:
    notes = []
    if constraints.commercial_use is None:
        notes.append("commercial use not stated: non-commercial licenses were kept and flagged")
    if "code" in needs:
        if constraints.programming_language is None:
            notes.append("no programming language given: repositories in any language were considered")
        if constraints.copyleft_ok is None:
            notes.append("copyleft (GPL-style) licenses were treated as acceptable")
    if "model" in needs:
        if not constraints.allow_pickle_weights:
            notes.append("models that ship only pickle weights were excluded (unsafe to load untrusted)")
        if constraints.max_model_size_gb is None:
            notes.append("no hardware limit given: model size did not affect the ranking")
    if {"model", "dataset"} & set(needs):
        if constraints.languages is None:
            notes.append("no natural language given: language coverage did not affect the ranking")
        if constraints.allow_gated is None:
            notes.append("gated models and datasets were kept and flagged")
    return notes


def _questions(constraints: Constraints, picks: list[Scored], contenders: list[Scored]) -> list[str]:
    """Questions worth putting to the user: only those whose answer would change the result."""
    questions = []
    if constraints.commercial_use is None:
        # Only candidates still in the running: one already rejected for another reason changes nothing.
        restricted = [
            s.candidate.id
            for s in contenders
            if not s.gate_failures
            and (s.candidate.license or s.candidate.license_reported)
            and classify(s.candidate.license) in ("non-commercial", "use-restricted")
        ]
        if restricted:
            count = (
                "1 of the leading candidates has"
                if len(restricted) == 1
                else f"{len(restricted)} of them have"
            )
            questions.append(
                f"Will this be used commercially? {count} a non-commercial or use-restricted license "
                f"(e.g. {restricted[0]})."
            )
    if constraints.programming_language is None:
        languages = _unique(
            [s.candidate.programming_language or "" for s in picks if s.candidate.need == "code"]
        )
        if len(languages) > 1:
            questions.append(
                f"Which programming language is the project in? The code picks are written in {', '.join(languages)}."
            )
    if constraints.max_model_size_gb is None:
        large = [
            s.candidate for s in picks if s.candidate.need == "model" and (s.candidate.size_bytes or 0) > 4e9
        ]
        if large:
            size = (large[0].size_bytes or 0) / 1e9
            questions.append(
                f"Is there a hardware limit? {large[0].id} needs about {size:.0f} GB for its weights."
            )
    return questions
