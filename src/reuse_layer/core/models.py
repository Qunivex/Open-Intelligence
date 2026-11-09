"""Schemas shared by the engine, the GitHub source and the adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

LicensePolicy = Literal["permissive", "weak_copyleft", "any_osi"]
Outcome = Literal["kept", "replaced", "removed", "tests_passed", "tests_failed"]
CiState = Literal["passing", "failing", "pending", "none", "unknown"]


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- requests (validated input)

Phrase = Annotated[str, Field(min_length=2, max_length=200)]
RepoRef = Annotated[str, Field(min_length=3, max_length=200)]
Query = Annotated[str, Field(min_length=2, max_length=256)]


class CallerContext(BaseModel):
    """The caller's constraints. Carries no source code and no secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str | None = Field(
        default=None, max_length=40, description="Primary language of the project, e.g. python, typescript."
    )
    license_policy: LicensePolicy | list[Annotated[str, Field(max_length=40)]] | None = Field(
        default=None,
        description="permissive (default), weak_copyleft, any_osi, or a list of allowed SPDX ids.",
    )
    allow_new: bool = Field(
        default=False, description="Allow projects younger than the minimum age (30 days)."
    )
    exclude: list[RepoRef] = Field(
        default_factory=list,
        max_length=20,
        description="owner/repo names to leave out (already tried, rejected).",
    )

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str | None) -> str | None:
        return v.strip().lower() or None if v else None


class FindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str = Field(min_length=3, max_length=600)
    capabilities: list[Phrase] = Field(default_factory=list, max_length=8)
    candidates: list[RepoRef] = Field(default_factory=list, max_length=15)
    search_queries: list[Query] = Field(default_factory=list, max_length=6)
    avoid: list[Phrase] = Field(default_factory=list, max_length=8)
    context: CallerContext = Field(default_factory=CallerContext)


class OutcomeReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation_id: str = Field(pattern=r"^rec_[0-9A-HJKMNP-TV-Z]{26}$")
    repo: RepoRef
    outcome: Outcome
    replaced_with: RepoRef | None = None
    note: str | None = Field(default=None, max_length=300)


# --------------------------------------------------------------------------- facts from GitHub


@dataclass(frozen=True)
class Repo:
    full_name: str  # canonical owner/name as GitHub reports it
    html_url: str
    description: str
    topics: tuple[str, ...]
    language: str | None
    license_spdx: str | None
    archived: bool
    disabled: bool
    fork: bool
    is_template: bool
    created_at: datetime
    pushed_at: datetime | None
    default_branch: str
    owner_login: str
    owner_type: str  # "User" | "Organization"
    stars: int
    forks: int
    open_issues: int
    homepage: str | None = None

    @property
    def name(self) -> str:
        return self.full_name.split("/", 1)[1]

    def age_days(self, now: datetime | None = None) -> int:
        return max(0, ((now or utcnow()) - self.created_at).days)

    def days_since_push(self, now: datetime | None = None) -> int | None:
        return None if self.pushed_at is None else max(0, ((now or utcnow()) - self.pushed_at).days)


@dataclass(frozen=True)
class Release:
    tag: str
    published_at: datetime | None
    prerelease: bool


@dataclass(frozen=True)
class Advisory:
    ghsa_id: str
    severity: str  # low | medium | high | critical | unknown
    summary: str
    patched: bool | None  # True: a fixed version exists; False: none; None: unknown
    malware: bool = False
    source: str = "repository"  # repository | github-advisory-database


@dataclass(frozen=True)
class Pin:
    ref: str  # release tag, tag, or default branch
    kind: Literal["release", "tag", "branch"]
    sha: str  # full commit SHA (the hash every pick is pinned to)
    signature: str  # valid | unsigned | <GitHub verification reason>


@dataclass
class DeepFacts:
    readme: str | None = None
    releases: list[Release] = field(default_factory=list)
    contributors: int | None = None
    contributors_capped: bool = False
    top_share: float | None = None
    health_pct: int | None = None
    advisories: list[Advisory] = field(default_factory=list)
    advisories_checked: bool = False
    org_verified: bool | None = None
    pin: Pin | None = None
    ci: CiState = "unknown"
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Card:
    """Capability card: a fixed schema built from repo text. Rankers read cards, never raw READMEs."""

    repo: str
    summary: str
    topics: tuple[str, ...]
    language: str | None
    features: tuple[str, ...]
    headings: tuple[str, ...]
    intro: str
    injection_suspected: bool = False


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    detail: str


@dataclass
class Scored:
    """One candidate as it moves through the pipeline."""

    repo: Repo
    found_by: set[str] = field(default_factory=set)
    deep: DeepFacts | None = None
    card: Card | None = None
    gates: list[GateResult] = field(default_factory=list)
    relevance: float = 0.0
    relevance_detail: list[str] = field(default_factory=list)
    trust: float = 0.0
    trust_detail: list[str] = field(default_factory=list)
    preference: float = 0.5
    preference_detail: str | None = None
    pref_good: int = 0
    pref_bad: int = 0
    verified: float = 0.0
    verified_detail: str = ""
    tier: int = 3
    tier_score: float = 0.0
    tier_detail: str = ""
    newer: bool = False
    probation: bool = False
    risks: list[str] = field(default_factory=list)
    total: float = 0.0
    explore: bool = False

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def first_failure(self) -> GateResult | None:
        return next((g for g in self.gates if not g.passed), None)
