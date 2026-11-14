"""The golden set: tasks, and graded judgments of candidates for each.

File format (TOML)::

    [meta]
    name = "seed"
    judged_by = "who graded these, and how"

    [[tasks]]
    id = "german-ner-dataset"
    need = "dataset"
    task = "Find a German NER dataset to fine-tune a token classifier"
    queries = ["german ner", "germeval"]
    must_have = ["named entity recognition", "german"]
    constraints = { languages = ["de"] }

    [tasks.judgments]            # "source:id" = grade
    "huggingface:GermanEval/germeval_14" = 3

Grades: 3 = the standard choice for the task, 2 = a good choice, 1 = usable with caveats,
0 = off-task, broken or not usable. Judge candidates by what a practitioner would build on,
not by what the ranking returned.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from open_intelligence.ranking import Constraints, RankRequest
from open_intelligence.sources import NEEDS, SOURCE_IDS


class GoldenError(ValueError):
    pass


@dataclass
class GoldenTask:
    id: str
    need: str
    task: str
    queries: list[str]
    must_have: list[str] = field(default_factory=list)
    constraints: Constraints = field(default_factory=lambda: Constraints())
    judgments: dict[str, int] = field(default_factory=dict)

    def request(self, top: int = 3) -> RankRequest:
        return RankRequest(
            task=self.task,
            query=self.queries[0],
            needs=[self.need],
            queries={self.need: list(self.queries)},
            must_have={self.need: list(self.must_have)} if self.must_have else {},
            constraints=self.constraints,
            top=top,
        )


@dataclass
class Golden:
    name: str
    judged_by: str
    tasks: list[GoldenTask]
    path: str = ""


def seed_path() -> Path:
    """The seed golden set shipped with the package."""
    return Path(str(resources.files("open_intelligence").joinpath("evaluation/seed.toml")))


def load_golden(path: str | Path | None = None) -> Golden:
    chosen = Path(path) if path is not None else seed_path()
    try:
        data = tomllib.loads(chosen.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GoldenError(f"cannot read {chosen}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise GoldenError(f"{chosen}: {exc}") from exc
    return golden_from_mapping(data, str(chosen))


def golden_from_mapping(data: dict[str, Any], path: str = "") -> Golden:
    meta = data.get("meta") or {}
    tasks = []
    seen: set[str] = set()
    for index, raw in enumerate(data.get("tasks") or []):
        where = f"task #{index + 1}"
        task_id = str(raw.get("id") or "").strip()
        if not task_id or task_id in seen:
            raise GoldenError(f"{where}: needs a unique id")
        seen.add(task_id)
        where = f"task {task_id!r}"
        need = raw.get("need")
        if need not in NEEDS:
            raise GoldenError(f"{where}: need must be one of {list(NEEDS)}")
        queries = [str(q).strip() for q in raw.get("queries") or [] if str(q).strip()]
        if not queries or not str(raw.get("task") or "").strip():
            raise GoldenError(f"{where}: needs a task and at least one query")
        try:
            constraints = Constraints(**(raw.get("constraints") or {}))
        except (ValidationError, TypeError) as exc:
            raise GoldenError(f"{where}: constraints are not valid: {exc}") from exc
        judgments: dict[str, int] = {}
        for key, grade in (raw.get("judgments") or {}).items():
            source = str(key).split(":", 1)[0]
            if source not in SOURCE_IDS or ":" not in str(key):
                raise GoldenError(f"{where}: judgment {key!r} must look like 'huggingface:owner/name'")
            if not isinstance(grade, int) or isinstance(grade, bool) or not 0 <= grade <= 3:
                raise GoldenError(f"{where}: grade of {key!r} must be an integer from 0 to 3")
            judgments[str(key)] = grade
        if not judgments:
            raise GoldenError(f"{where}: has no judgments")
        tasks.append(
            GoldenTask(
                id=task_id,
                need=need,
                task=str(raw["task"]).strip(),
                queries=queries,
                must_have=[str(m).strip() for m in raw.get("must_have") or [] if str(m).strip()],
                constraints=constraints,
                judgments=judgments,
            )
        )
    if not tasks:
        raise GoldenError("the golden set has no tasks")
    return Golden(
        name=str(meta.get("name") or "unnamed"),
        judged_by=str(meta.get("judged_by") or "not stated"),
        tasks=tasks,
        path=path,
    )
