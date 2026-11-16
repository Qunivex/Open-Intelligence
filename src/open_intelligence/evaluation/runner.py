"""Run a golden set through the ranking and through each platform's own order, and compare.

For every task the runner records:

* the ranking's top 3 and its nDCG@3 / precision@3 against the judgments;
* the same metrics for what each platform's default order showed first (the baseline the
  ranking has to beat to be worth its complexity);
* with ``ablations``, the mean nDCG@3 when each score part is switched off in turn.

Live results change from day to day. A ``Recorder`` remembers every upstream answer, so a run
can be saved and replayed: the same data, scored by different code or weights.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.shared._httpx_utils import create_mcp_http_client

from open_intelligence.config import RankingSettings, Settings
from open_intelligence.connector import CallOutcome, Connector
from open_intelligence.evaluation.golden import Golden, GoldenTask
from open_intelligence.evaluation.metrics import judged_at_k, ndcg_at_k, precision_at_k
from open_intelligence.ranking.engine import Ranker, Upstream
from open_intelligence.ranking.evidence import SecurityLookup
from open_intelligence.ranking.scoring import PARTS

K = 3


class Recorder:
    """Stands in for the connector and remembers every upstream answer.

    Live, it forwards what it has not seen to the real connector and keeps the answer. In replay
    it answers only from a saved recording, and anything missing fails as "not in the recording".
    """

    def __init__(self, connector: Connector, recording: dict[str, Any] | None = None) -> None:
        self._connector = connector
        self.settings: Settings = connector.settings
        self.replay_only = recording is not None
        recording = recording or {}
        self.calls: dict[str, dict[str, Any]] = dict(recording.get("calls") or {})
        self.fetches: dict[str, list[Any]] = dict(recording.get("fetches") or {})
        when = recording.get("recorded_at")
        self.recorded_at = datetime.fromisoformat(when) if when else datetime.now(UTC)

    @staticmethod
    def _key(source: str, tool: str, arguments: dict[str, Any]) -> str:
        return json.dumps([source, tool, arguments], sort_keys=True, ensure_ascii=False)

    async def call_many(
        self,
        source_id: str,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        full_text: bool = False,
        concurrency: int = 1,
    ) -> list[CallOutcome]:
        keys = [self._key(source_id, tool, arguments) for tool, arguments in calls]
        # Live, a remembered failure is asked again: a missing token or an outage is not an answer.
        missing = [
            i
            for i, key in enumerate(keys)
            if key not in self.calls or (not self.replay_only and not self.calls[key]["ok"])
        ]
        if missing and not self.replay_only:
            fresh = await self._connector.call_many(
                source_id, [calls[i] for i in missing], full_text=True, concurrency=concurrency
            )
            for i, outcome in zip(missing, fresh, strict=True):
                self.calls[keys[i]] = {
                    "ok": outcome.ok,
                    "text": outcome.text,
                    "structured": outcome.structured,
                    "error": outcome.error,
                }
        outcomes = []
        for key, (tool, arguments) in zip(keys, calls, strict=True):
            saved = self.calls.get(key) or {
                "ok": False,
                "text": "",
                "structured": None,
                "error": "not in the recording",
            }
            outcomes.append(
                CallOutcome(
                    source=source_id,
                    tool=tool,
                    arguments=arguments,
                    ok=bool(saved["ok"]),
                    text=saved.get("text") or "",
                    structured=saved.get("structured"),
                    error=saved.get("error"),
                )
            )
        return outcomes

    async def fetch(self, url: str) -> tuple[int, Any]:
        """The same, for the plain HTTP lookups of the security evidence."""
        if url not in self.fetches:
            if self.replay_only:
                return 0, None
            try:
                async with create_mcp_http_client() as http:
                    response = await http.get(url)
                try:
                    self.fetches[url] = [response.status_code, response.json()]
                except ValueError:
                    self.fetches[url] = [response.status_code, None]
            except Exception:
                return 0, None  # not kept: a network failure is not an answer
        status, data = self.fetches[url]
        return int(status), data

    def now(self) -> datetime:
        return self.recorded_at

    def security(self) -> SecurityLookup:
        return SecurityLookup(self.settings.evidence, fetch=self.fetch)

    def save(self, path: str | Path) -> None:
        data = {"recorded_at": self.recorded_at.isoformat(), "calls": self.calls, "fetches": self.fetches}
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        Path(path).write_bytes(gzip.compress(raw) if str(path).endswith(".gz") else raw)

    @classmethod
    def load(cls, path: str | Path, connector: Connector) -> Recorder:
        raw = Path(path).read_bytes()
        if str(path).endswith(".gz"):
            raw = gzip.decompress(raw)
        return cls(connector, json.loads(raw.decode("utf-8")))


@dataclass
class Scores:
    ndcg: float | None
    precision: float
    judged: float

    def as_dict(self) -> dict[str, float | None]:
        return {
            "ndcg_at_3": None if self.ndcg is None else round(self.ndcg, 3),
            "precision_at_3": round(self.precision, 3),
            "judged_at_3": round(self.judged, 3),
        }


def score(ranked: list[str], task: GoldenTask) -> Scores:
    return Scores(
        ndcg=ndcg_at_k(ranked, task.judgments, K),
        precision=precision_at_k(ranked, task.judgments, K),
        judged=judged_at_k(ranked, task.judgments, K),
    )


@dataclass
class TaskResult:
    id: str
    need: str
    verdict: str
    ranked: list[str] = field(default_factory=list)
    scores: Scores | None = None  # None when the task could not be run
    platforms: dict[str, tuple[list[str], Scores]] = field(default_factory=dict)
    note: str = ""

    @property
    def best_platform(self) -> float | None:
        """The better of the platforms' own top 3: the generous baseline."""
        values = [s.ndcg for _, s in self.platforms.values() if s.ndcg is not None]
        return max(values) if values else None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "need": self.need, "verdict": self.verdict}
        if self.scores is None:
            out["skipped"] = self.note
            return out
        out["ranking"] = {"top": self.ranked, **self.scores.as_dict()}
        out["platforms"] = {
            source: {"top": top, **scores.as_dict()} for source, (top, scores) in self.platforms.items()
        }
        return out


def _mean(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else None


@dataclass
class Report:
    golden: str
    judged_by: str
    weights: dict[str, float]
    when: str
    tasks: list[TaskResult]
    ablations: dict[str, float | None] = field(default_factory=dict)  # part -> mean nDCG@3 without it

    @property
    def scored(self) -> list[TaskResult]:
        return [t for t in self.tasks if t.scores is not None]

    @property
    def ranking_ndcg(self) -> float | None:
        return _mean([t.scores.ndcg for t in self.scored if t.scores])

    @property
    def platform_ndcg(self) -> float | None:
        return _mean([t.best_platform for t in self.scored])

    @property
    def ranking_precision(self) -> float | None:
        return _mean([t.scores.precision for t in self.scored if t.scores])

    @property
    def platform_precision(self) -> float | None:
        best = [max((s.precision for _, s in t.platforms.values()), default=None) for t in self.scored]
        return _mean(best)

    def as_dict(self) -> dict[str, Any]:
        def rounded(value: float | None) -> float | None:
            return None if value is None else round(value, 3)

        return {
            "golden": self.golden,
            "judged_by": self.judged_by,
            "when": self.when,
            "weights": {k: round(v, 3) for k, v in self.weights.items()},
            "tasks_scored": len(self.scored),
            "tasks_skipped": len(self.tasks) - len(self.scored),
            "mean_ndcg_at_3": {
                "ranking": rounded(self.ranking_ndcg),
                "best_platform_order": rounded(self.platform_ndcg),
            },
            "mean_precision_at_3": {
                "ranking": rounded(self.ranking_precision),
                "best_platform_order": rounded(self.platform_precision),
            },
            "ablations_mean_ndcg_at_3_without": {k: rounded(v) for k, v in self.ablations.items()},
            "tasks": [t.as_dict() for t in self.tasks],
        }


async def _run(
    golden: Golden,
    connector: Upstream,
    settings: RankingSettings,
    now: Callable[[], datetime] | None,
    security: SecurityLookup | None,
) -> list[TaskResult]:
    results = []
    for task in golden.tasks:
        ranker = Ranker(connector, settings=settings, now=now, security=security)
        outcome = await ranker.find(task.request(K))
        entry = next((r for r in outcome["results"] if r["need"] == task.need), None)
        if entry is None:
            results.append(
                TaskResult(task.id, task.need, "not_routed", note="the router did not search this need")
            )
            continue
        result = TaskResult(task.id, task.need, entry["verdict"])
        if entry["verdict"] in ("inconclusive", "not_searched"):
            errors = [e for s in entry.get("sources", []) for e in s.get("errors", [])]
            result.note = errors[0] if errors else entry.get("note", "")
            results.append(result)
            continue
        result.ranked = [f"{p['source']}:{p['id']}" for p in entry["picks"]]
        result.scores = score(result.ranked, task)
        for source, ids in entry.get("platform_top", {}).items():
            top = [f"{source}:{i}" for i in ids]
            result.platforms[source] = (top, score(top, task))
        results.append(result)
    return results


async def evaluate(
    golden: Golden,
    connector: Upstream,
    *,
    settings: RankingSettings | None = None,
    now: Callable[[], datetime] | None = None,
    security: SecurityLookup | None = None,
    ablations: bool = True,
) -> Report:
    settings = settings or connector.settings.ranking
    tasks = await _run(golden, connector, settings, now, security)
    report = Report(
        golden=golden.name,
        judged_by=golden.judged_by,
        weights=dict(settings.weights),
        when=(now() if now else datetime.now(UTC)).date().isoformat(),
        tasks=tasks,
    )
    if ablations:
        kept = {t.id for t in report.scored}
        for part in PARTS:
            if settings.weights[part] <= 0:
                continue
            weights = {**settings.weights, part: 0.0}
            total = sum(weights.values())
            without = replace(settings, weights={k: v / total for k, v in weights.items()})
            rerun = await _run(golden, connector, without, now, security)
            report.ablations[part] = _mean([t.scores.ndcg for t in rerun if t.id in kept and t.scores])
    return report


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def render_markdown(report: Report) -> str:
    lines = [
        f"Golden set: **{report.golden}** ({len(report.scored)} tasks scored, "
        f"{len(report.tasks) - len(report.scored)} skipped). Judged by: {report.judged_by}",
        "",
        f"Data as of {report.when}. Weights: "
        + ", ".join(f"{name} {report.weights[name]:.0%}" for name in PARTS)
        + ".",
        "",
        "| task | need | ranking nDCG@3 | best platform order nDCG@3 | ranking P@3 | judged@3 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for t in report.tasks:
        if t.scores is None:
            lines.append(f"| {t.id} | {t.need} | skipped | | | |")
            continue
        lines.append(
            f"| {t.id} | {t.need} | {_fmt(t.scores.ndcg)} | {_fmt(t.best_platform)} | "
            f"{_fmt(t.scores.precision)} | {_fmt(t.scores.judged)} |"
        )
    lines += [
        f"| **mean** | | **{_fmt(report.ranking_ndcg)}** | **{_fmt(report.platform_ndcg)}** | "
        f"**{_fmt(report.ranking_precision)}** | |",
        "",
    ]
    skipped = [t for t in report.tasks if t.scores is None]
    if skipped:
        lines.append("Skipped: " + "; ".join(f"{t.id} ({t.note or t.verdict})" for t in skipped))
        lines.append("")
    if report.ablations:
        full = report.ranking_ndcg
        lines += [
            "| score part removed | mean nDCG@3 without it | change |",
            "| --- | ---: | ---: |",
        ]
        for part, value in report.ablations.items():
            change = "n/a" if value is None or full is None else f"{value - full:+.2f}"
            lines.append(f"| {part} | {_fmt(value)} | {change} |")
        lines.append("")
        lines.append(
            "A negative change means the part helps (removing it hurts); near zero means it does not "
            "change the top 3 on this set."
        )
    return "\n".join(lines)
