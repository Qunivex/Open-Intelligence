"""Schemas shared by the engine, the GitHub source and the adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

LicensePolicy = Literal["permissive", "weak_copyleft", "any_osi"]
Outcome = Literal["kept", "replaced", "removed", "tests_passed", "tests_failed"]
CiState = Literal["passing", "failing", "pending", "none", "unknown"]
# Which refs a pick may be pinned to. The user chooses (the discovery skill asks once):
#   standard  a stable release with a plain version tag (v1.2.3, 1.2.3), else the default branch head
#   all       the highest version of any kind: pre-releases and any tag naming scheme included
RefPolicy = Literal["standard", "all"]
# Whether a pick is also published, at its pinned version, on its language's package registry.
PackageStatus = Literal["published", "yanked", "not_published", "not_linked", "unknown", "unsupported"]
# What can be searched for. One source adapter serves each type (see ``sources/base.py``); adding
# an adapter adds its type here, which is how callers learn that it exists.
ArtifactType = Literal["repository"]


def utcnow() -> datetime:
    return datetime.now(UTC)


def recent(dt: datetime | None, days: int, now: datetime | None = None) -> bool:
    return dt is not None and (now or utcnow()) - dt <= timedelta(days=days)


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
    ref_policy: RefPolicy = Field(
        default="standard",
        description=(
            "Which version of each pick to pin, as the user chose. standard (default): the latest stable "
            "release with a plain version tag (v1.2.3, 1.2.3); a project with none is pinned to its default "
            "branch head (main/master). all: the highest version of any kind, including pre-releases (rc, "
            "beta) and tags in other naming schemes (pkg@1.2.3, release-1.2.3)."
        ),
    )

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str | None) -> str | None:
        return v.strip().lower() or None if v else None


class FindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str = Field(min_length=3, max_length=600)
    # One call decides one component of the caller's project; the name is only echoed back.
    component: str | None = Field(default=None, min_length=1, max_length=80)
    # Every capability is a must-have: a pick that does not evidence one cannot get a plain `reuse`.
    capabilities: list[Phrase] = Field(default_factory=list, max_length=8)
    candidates: list[RepoRef] = Field(default_factory=list, max_length=15)
    search_queries: list[Query] = Field(default_factory=list, max_length=6)
    avoid: list[Phrase] = Field(default_factory=list, max_length=8)
    context: CallerContext = Field(default_factory=CallerContext)
    artifact_type: ArtifactType = "repository"


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


@dataclass(frozen=True)
class Pin:
    ref: str  # release tag, tag, or default branch
    kind: Literal["release", "tag", "branch"]
    sha: str  # full commit SHA (the hash every pick is pinned to)
    signature: str  # valid | unsigned | <GitHub verification reason>
    committed_at: datetime | None = None
    prerelease: bool = False  # a pre-release or a pre-release tag (only under ref_policy "all")
    # Releases and version tags the ref policy passed over (under "standard": pre-releases and
    # non-standard tag names). Non-zero on a branch pin means the project does publish versions.
    skipped: int = 0

    @property
    def version_kind(self) -> str:
        """release | prerelease | tag | branch: what a person needs to know about the pinned ref."""
        return "prerelease" if self.prerelease else self.kind


@dataclass(frozen=True)
class Package:
    """The pick's package on its language's official registry, at the pinned version.

    Informational: it is never a gate and never scored. ``published`` needs the registry's own
    metadata to name this repository as the package's source; a package that only shares the name
    is ``not_linked``, because anyone can register a name.
    """

    status: PackageStatus
    detail: str = ""
    registry: str | None = None  # "PyPI", "npm", ...
    name: str | None = None
    version: str | None = None
    install: str | None = None
    # The registry names the commit it was built from: does it match the pinned revision? None when
    # the registry does not say.
    same_commit: bool | None = None
    # The directory the package is published from, when it is not the repository root (a monorepo).
    path: str | None = None
    # True: the registry holds a build attestation (or trusted-publishing record) naming this
    # repository. False: the registry records these and has none for this version. None: the registry
    # records none of either kind, or it was not checked.
    attested: bool | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "registry": self.registry,
            "name": self.name,
            "version": self.version,
            "path": self.path,
            "status": self.status,
            "install": self.install,
            "same_commit": self.same_commit,
            "attested": self.attested,
            "detail": self.detail,
        }
        return {k: v for k, v in out.items() if v not in (None, "")}


@dataclass
class DeepFacts:
    readme: str | None = None
    releases: list[Release] = field(default_factory=list)
    default_branch_commit_at: datetime | None = None
    contributors: int | None = None
    contributors_capped: bool = False
    top_share: float | None = None
    health_pct: int | None = None
    advisories: list[Advisory] = field(default_factory=list)
    advisories_checked: bool = False
    # Advisories for a package with this repository's name that name no repository at all: they
    # can be neither attributed to this project nor ruled out.
    advisories_unattributed: int = 0
    org_verified: bool | None = None
    pin: Pin | None = None
    ci: CiState = "unknown"
    # Filled for the final picks only, after ranking; None when no registry check ran.
    package: Package | None = None
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
    # False when the check could not be completed. Such a gate is never passed (it fails closed),
    # but the candidate is "unchecked", which is a different finding from "failed".
    complete: bool = True


@dataclass
class Scored:
    """One candidate as it moves through the pipeline."""

    repo: Repo
    found_by: set[str] = field(default_factory=set)
    deep: DeepFacts | None = None
    deep_error: str | None = None  # why the evidence pass did not run: time budget, rate limit, error
    card: Card | None = None
    covered: tuple[str, ...] = ()  # must-have capabilities the card evidences
    found_in: tuple[str, ...] = ()  # for each covered one, "metadata" or "readme"
    missing: tuple[str, ...] = ()  # must-have capabilities it does not
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
    unresolved_checks: list[str] = field(default_factory=list)  # unknown even though it passed
    total: float = 0.0
    explore: bool = False

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    @property
    def unchecked(self) -> bool:
        """Rejected only because checks could not complete: no gate found an actual problem."""
        failed = [g for g in self.gates if not g.passed]
        return bool(failed) and not any(g.complete for g in failed)

    def first_failure(self) -> GateResult | None:
        """The gate to report for a rejection: a real finding first, else the first unchecked gate."""
        failed = [g for g in self.gates if not g.passed]
        return next((g for g in failed if g.complete), failed[0] if failed else None)
