"""Signals borrowed from GitHub: trust, owner tier, upstream verification, newness, lookalikes.

Stars are deliberately absent from every score: they are cheap to buy. They
only break exact ties, and a burst of stars on a young project is a reason to
hold it in probation, not to promote it.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Sequence
from datetime import datetime
from functools import lru_cache
from importlib import resources

from reuse_layer.core.models import DeepFacts, Repo, Scored, recent


@lru_cache(maxsize=1)
def registry_curated_owners() -> frozenset[str]:
    data = tomllib.loads(resources.files("reuse_layer.sources").joinpath("registry.toml").read_text("utf-8"))
    owners: set[str] = set()
    for group in (data.get("curated_owners") or {}).values():
        owners.update(str(o).lower() for o in group if isinstance(o, str))
    return frozenset(owners)


def trust(repo: Repo, deep: DeepFacts, now: datetime) -> tuple[float, list[str], list[str]]:
    """(score 0-1, evidence, risks). Do people keep this project healthy and released?"""
    evidence: list[str] = []
    risks: list[str] = []

    c = deep.contributors
    if c is None:
        maint = 0.3
    else:
        maint = min(1.0, math.log1p(c) / math.log1p(30))
        line = f"{c}{'+' if deep.contributors_capped else ''} contributors"
        if deep.top_share is not None and c > 1:
            line += f" (top contributor {deep.top_share:.0%} of commits)"
            if deep.top_share > 0.85:
                maint *= 0.75
                risks.append("Bus factor: one person makes most commits")
        evidence.append(line)
        if c <= 1:
            risks.append("Single maintainer")

    d = repo.days_since_push(now)
    activity = 0.0 if d is None else (1.0 if d <= 30 else max(0.0, 1 - (d - 30) / 700))
    if d is not None:
        evidence.append("last push today" if d == 0 else f"last push {d} day{'s' if d != 1 else ''} ago")

    stable = [r for r in deep.releases if not r.prerelease]
    in_year = [r for r in stable if recent(r.published_at, 365, now)]
    unread = {e.split(":", 1)[0] for e in deep.errors} & {"releases", "tags"}
    if stable:
        cadence = min(1.0, len(in_year) / 6)
        latest = stable[0]
        when = latest.published_at.date().isoformat() if latest.published_at else "date unknown"
        evidence.append(f"{len(in_year)} releases in the last year (latest {latest.tag}, {when})")
    elif unread:
        # Not being able to list releases is not the same as there being none: say which it was.
        cadence = 0.0
        ref = f"{deep.pin.kind} {deep.pin.ref}" if deep.pin else "nothing"
        risks.append(f"Release history could not be read: pinned to {ref}, which may not be the latest")
    elif deep.pin is not None and deep.pin.kind == "tag":
        cadence = 0.3
        evidence.append(f"versioned tags, no GitHub releases (latest {deep.pin.ref})")
    elif deep.pin is not None and deep.pin.skipped:
        # It does tag versions, just none the caller's ref policy accepts: the source says which.
        cadence = 0.3
    else:
        cadence = 0.0
        risks.append("No releases or version tags: pinned to a branch commit")

    years = repo.age_days(now) / 365
    longevity = min(1.0, years / 3)
    evidence.append(f"{years:.1f} years old" if years >= 1 else f"{repo.age_days(now)} days old")

    health = (deep.health_pct / 100) if deep.health_pct is not None else 0.4
    if deep.health_pct is not None:
        evidence.append(f"community health {deep.health_pct}%")

    score = 0.35 * maint + 0.25 * activity + 0.15 * cadence + 0.15 * longevity + 0.10 * health
    return round(score, 3), evidence, risks


def tier(repo: Repo, deep: DeepFacts | None, curated: frozenset[str]) -> tuple[int, float, str]:
    owner = repo.owner_login.lower()
    if owner in curated:
        return 1, 1.0, "curated owner (foundation, government or public-goods body)"
    if deep is not None and deep.org_verified:
        return 2, 0.6, "verified organization"
    if repo.owner_type == "Organization":
        return 3, 0.4, "organization (unverified)"
    return 3, 0.25, "personal account"


def verified(deep: DeepFacts) -> tuple[float, str]:
    """Upstream CI on the pinned commit. v1 has no sandbox, so this is the project's own evidence."""
    return {
        "passing": (1.0, "upstream CI passing on the pinned commit"),
        "failing": (0.0, "upstream CI failing on the pinned commit"),
        "pending": (0.4, "upstream CI still running on the pinned commit"),
        "none": (0.3, "no CI checks on the pinned commit"),
    }.get(deep.ci, (0.4, "CI status unknown"))


def is_newer(repo: Repo, newer_than_days: int, now: datetime) -> bool:
    return repo.age_days(now) < newer_than_days


def sudden_riser(repo: Repo, deep: DeepFacts | None, now: datetime) -> bool:
    """Many stars, very young, few contributors: the pattern of bought or burst stars."""
    age = max(1, repo.age_days(now))
    contributors = deep.contributors if deep and deep.contributors is not None else 0
    return age < 180 and repo.stars / age > 40 and contributors < 5


def _distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 3
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _norm(name: str) -> str:
    return name.lower().replace("-", "").replace("_", "").replace(".", "")


def lookalikes(pool: Sequence[Scored]) -> dict[str, str]:
    """Map repo -> the more established repo its name imitates (different owner, near-identical name)."""
    out: dict[str, str] = {}
    for s in pool:
        for other in pool:
            if other is s or other.repo.owner_login.lower() == s.repo.owner_login.lower():
                continue
            a, b = _norm(s.repo.name), _norm(other.repo.name)
            if len(a) < 4 or (a == b and s.repo.name.lower() == other.repo.name.lower()):
                # same exact name under different owners is common (forks, mirrors); the fork gate handles forks
                continue
            close = a == b or _distance(a, b) == 1 or (a.startswith(b) and len(a) - len(b) <= 3)
            if close and _more_established(other, s):
                out[s.repo.full_name] = other.repo.full_name
    return out


def _more_established(a: Scored, b: Scored) -> bool:
    return a.repo.created_at < b.repo.created_at and a.repo.forks >= 5 * max(1, b.repo.forks)
