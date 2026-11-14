"""Records shared by the ranking pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from open_intelligence.sources import Need


class Constraints(BaseModel):
    """What the user's situation requires. Leave a field out when it is unknown: nothing is assumed
    silently, and the result lists every assumption made and any question worth asking the user."""

    model_config = ConfigDict(extra="forbid")

    programming_language: str | None = Field(
        default=None, description="Language of the user's project, e.g. 'python'. Used for code."
    )
    commercial_use: bool | None = Field(
        default=None,
        description="True if the result will be used commercially: non-commercial licenses are excluded.",
    )
    copyleft_ok: bool | None = Field(
        default=None, description="False if GPL/AGPL-style licenses are not acceptable. Default: acceptable."
    )
    libraries: list[str] | None = Field(
        default=None,
        description="ML libraries / formats the user can run, e.g. ['transformers'], ['gguf'], ['onnx'].",
    )
    languages: list[str] | None = Field(
        default=None, description="Natural languages the model or data must cover, as ISO codes, e.g. ['de']."
    )
    max_model_size_gb: float | None = Field(
        default=None, description="Largest model weights the user's hardware can hold, in GB."
    )
    allow_gated: bool | None = Field(
        default=None,
        description="False to exclude models/datasets that need an access request. Default: allowed.",
    )
    allow_pickle_weights: bool | None = Field(
        default=None,
        description="True to allow models that ship only pickle weights (can run code when loaded). Default: excluded.",
    )
    max_age_months: int | None = Field(
        default=None, description="Exclude anything not updated within this many months."
    )

    def given(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


@dataclass
class EvalResult:
    """One evaluation result a model card declares (self-reported unless ``verified``)."""

    dataset: str
    metric: str
    value: float | None
    verified: bool = False
    config: str = ""
    split: str = ""

    @property
    def benchmark(self) -> tuple[str, str, str, str]:
        """What the number was measured on. Two results are comparable only when all four agree:
        accuracy on a training split says nothing about accuracy on a validation split."""

        def norm(text: str) -> str:
            return text.strip().lower().replace("_", "-")

        return (norm(self.dataset), norm(self.config), norm(self.split), norm(self.metric))

    @property
    def benchmark_name(self) -> str:
        where = " ".join(p for p in (self.dataset, self.config, self.split) if p)
        return f"{self.metric} on {where}"

    def describe(self) -> str:
        value = "" if self.value is None else f" {self.value:.4g}"
        tag = "verified by Hugging Face" if self.verified else "self-reported"
        return f"{self.metric}{value} on {self.dataset} ({tag})"


@dataclass
class Advisory:
    """One known vulnerability that affects the version a user would install today."""

    id: str
    title: str = ""
    cvss: float | None = None

    @property
    def severity(self) -> str:
        if self.cvss is None:
            return "unknown severity"
        if self.cvss >= 9:
            return "critical"
        if self.cvss >= 7:
            return "high"
        return "moderate" if self.cvss >= 4 else "low"


@dataclass
class SecurityEvidence:
    """What OpenSSF Scorecard and the advisory databases say about a repository."""

    scorecard: float | None = None  # 0..10
    scorecard_date: str | None = None
    scorecard_checks: dict[str, int] = field(default_factory=dict)
    package: str | None = None  # the published package the advisories were checked for, e.g. "pypi:flask"
    package_version: str | None = None
    advisories: list[Advisory] | None = None  # None = not checked; [] = none affect that version
    notes: list[str] = field(default_factory=list)  # what could not be checked, and why


@dataclass
class DatasetStructure:
    """What a dataset's files look like, as far as the platform reports it."""

    splits: dict[str, float | None] = field(default_factory=dict)  # "config/split" -> rows, if known
    total_rows: float | None = None
    columns: list[str] = field(default_factory=list)
    column_count: int | None = None
    label_names: list[str] = field(default_factory=list)  # classes of a ClassLabel column
    file_types: list[str] = field(default_factory=list)
    preview_of: tuple[str, str] | None = None  # (config, split) to read sample rows from
    sample_rows: int | None = None  # rows looked at
    sample_empty_cells: int | None = None
    sample_duplicate_rows: int | None = None

    @property
    def split_names(self) -> set[str]:
        return {name.rsplit("/", 1)[-1].lower() for name in self.splits}

    @property
    def has_held_out_split(self) -> bool | None:
        if not self.splits:
            return None
        return bool(self.split_names & {"test", "validation", "val", "dev", "eval"})


@dataclass
class Candidate:
    """One repository, model, dataset or notebook, in a shape every source is mapped to.

    ``None`` always means "not known", never "no": an unknown stays unknown through scoring and is
    reported, instead of being counted for or against the candidate.
    """

    source: str
    need: Need
    id: str
    url: str
    title: str = ""
    owner: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    task: str | None = None
    library: str | None = None
    languages: list[str] = field(default_factory=list)  # natural languages
    programming_language: str | None = None
    license: str | None = None  # as the platform reports it
    license_reported: bool = False  # the platform told us (even if what it said is "none")
    updated_at: datetime | None = None
    created_at: datetime | None = None
    released_at: datetime | None = None
    has_releases: bool | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)
    base_model: str | None = None
    lineage: str | None = None  # finetune | quantized | adapter | merge
    weight_formats: set[str] | None = None
    size_bytes: int | None = None  # one copy of a model's weights, or a dataset's files
    params: float | None = None
    doc_text: str = ""  # README / model card / dataset card, without front matter
    evals: list[EvalResult] = field(default_factory=list)
    security: SecurityEvidence | None = None  # repositories; None = not looked up
    structure: DatasetStructure | None = None  # datasets; None = not looked up
    enriched: bool = False
    read_notes: list[str] = field(default_factory=list)  # what could not be read
    found_by: list[str] = field(default_factory=list)
    position: int = 10_000  # best position in a platform's own default ordering (0 = first)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def name(self) -> str:
        return self.id.rsplit("/", 1)[-1]


@dataclass
class Call:
    """One upstream tool call planned by an adapter."""

    tool: str
    arguments: dict[str, Any]
    label: str = ""
    baseline: bool = False  # its result order is the platform's own default order
    targets: list[str] = field(default_factory=list)  # candidate ids this call enriches
