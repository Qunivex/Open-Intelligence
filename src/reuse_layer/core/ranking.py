"""The ranking formula from the design, applied at request time.

    score(p) = gate(p, repo) * [a*rel + b*trust + c*pref + d*verified + e*tier] + explore(p)

* ``gate`` is 0 or 1 and comes only from hard checks.
* Starting weights a..e = 0.35, 0.25, 0.20, 0.10, 0.10 (configurable).
* Stars carry no weight; they only break exact ties.
* At most one of the three slots goes to a newer project that passes every
  gate, chosen by Thompson sampling over its recorded outcomes, so good new
  projects can earn trust. That pick is marked ``explore``.
"""

from __future__ import annotations

import math
import random
from typing import Literal

from reuse_layer.config import RankingSettings
from reuse_layer.core.models import Scored

Verdict = Literal["reuse", "build_yourself", "no_candidates"]


def total(s: Scored, w: RankingSettings) -> float:
    if not s.passed:
        return 0.0
    weights = w.relevance + w.trust + w.preference + w.verified + w.tier
    raw = (
        w.relevance * s.relevance
        + w.trust * s.trust
        + w.preference * s.preference
        + w.verified * s.verified
        + w.tier * s.tier_score
    )
    return round(raw / weights, 4) if weights else 0.0


def _key(s: Scored) -> tuple[float, float]:
    return round(s.total, 3), math.log1p(s.repo.stars)  # stars only break ties


def select(scored: list[Scored], w: RankingSettings, rng: random.Random, k: int = 3) -> list[Scored]:
    passed = [s for s in scored if s.passed]
    established = sorted((s for s in passed if not (s.newer or s.probation)), key=_key, reverse=True)
    newer = [s for s in passed if s.newer or s.probation]
    picks = established[:k]
    if w.explore and newer:
        floor = max(0.45, picks[-1].total * 0.85 if len(picks) == k else 0.45)
        eligible = [s for s in newer if s.total >= floor and s.relevance >= w.build_yourself_below]
        if eligible:
            choice = max(eligible, key=lambda s: s.total * rng.betavariate(1 + s.pref_good, 1 + s.pref_bad))
            choice.explore = True
            picks = [*picks[: k - 1], choice] if len(picks) == k else [*picks, choice]
    return sorted(picks, key=_key, reverse=True)


def verdict(pool_size: int, picks: list[Scored], w: RankingSettings) -> tuple[Verdict, bool, str]:
    """(verdict, build_yourself_recommended, reason)."""
    if pool_size == 0:
        return "no_candidates", True, "nothing on GitHub matched; widen the search or build it"
    if not picks:
        return "no_candidates", True, "every candidate failed a security or fit gate (see rejected)"
    best = max(picks, key=lambda s: s.relevance)
    if best.relevance < w.build_yourself_below:
        return (
            "build_yourself",
            True,
            f"closest match {best.repo.full_name} fits weakly (relevance {best.relevance:.2f}); "
            "a small custom implementation is likely cheaper than adapting it",
        )
    n = sum(1 for p in picks if p.relevance >= w.build_yourself_below)
    return "reuse", False, f"{n} maintained, gate-passing project{'s' if n != 1 else ''} cover this task"
