"""The ranking formula from the design, applied at request time, and the verdict rules.

    score(p) = gate(p, repo) * [a*rel + b*trust + c*pref + d*verified + e*tier] + explore(p)

* ``gate`` is 0 or 1 and comes only from hard checks.
* Starting weights a..e = 0.35, 0.25, 0.20, 0.10, 0.10 (configurable).
* Stars carry no weight; they only break exact ties.
* At most one of the three slots goes to a newer project that passes every
  gate, chosen by Thompson sampling over its recorded outcomes, so good new
  projects can earn trust. That pick is marked ``explore``.

Must-have coverage comes before the score
-----------------------------------------
Every capability the caller lists is a must-have. The deterministic lexical
judge marks one *covered* only when the capability card evidences it at
``LexicalJudge.COVERED`` (0.6) or better; anything less is *missing*, meaning
"not evidenced in the README or metadata", which is weaker than "absent".
Picks are ordered by how many must-haves they leave missing (fewest first)
and only then by score. Trust, track record, owner tier and the agreement
bonus therefore cannot lift a project with a gap above one without it. The
optional model judge changes the relevance score only: it can lower fit, but
it cannot mark a capability covered.

Verdicts
--------
One call decides one component. The verdict is about the *recommended* pick:
the first pick, in the order above, whose relevance is at least
``build_yourself_below`` (0.40) and which evidences at least one must-have.

==================  =========================================================
``reuse``           The recommended pick evidences every must-have (or none
                    were given) and no gate check found a blocking issue.
``reuse_partial``   A recommended pick exists but leaves at least one
                    must-have missing. Reuse it for the covered part; the
                    missing part still has to be found or built.
``build_yourself``  Decisive evidence is complete and no gate-passing
                    candidate qualifies as a recommended pick.
``no_candidates``   Decisive evidence is complete, every candidate found was
                    examined, and none passed the gates (or nothing matched).
``inconclusive``    None of the above can be supported because something
                    decisive could not be checked: a search or a candidate
                    lookup failed or was rate limited, evidence checks did
                    not run or could not complete, or candidates were left
                    unexamined. It is not a "no": retry, do not build on it.
==================  =========================================================

Conservative defaults, deliberately not configurable:

* ``reuse`` needs 100% of the must-haves; one gap makes it ``reuse_partial``.
* A negative verdict (``build_yourself``, ``no_candidates``) is given only on
  complete decisive evidence; otherwise the verdict is ``inconclusive`` and
  build-it-yourself is not recommended.
* A positive verdict may be given on partial evidence, because every pick
  passed its own gates on checks that completed. The reason then says the
  evidence is partial and that a better candidate may have been missed.

No verdict, gate or score says a project is safe or compatible. They report
what the listed checks found, and the wording produced here keeps to that.
"""

from __future__ import annotations

import math
import random
from typing import Literal, NamedTuple

from reuse_layer.config import RankingSettings
from reuse_layer.core.models import Scored

Verdict = Literal["reuse", "reuse_partial", "build_yourself", "no_candidates", "inconclusive"]

NEXT_STEP: dict[Verdict, str] = {
    "reuse": "Integrate at the pinned commit, run your own tests, then call report_outcome.",
    "reuse_partial": "Reuse it for the covered capabilities; find the missing ones as their own component "
    "or build them.",
    "build_yourself": "Build it, or search again with different vocabulary.",
    "no_candidates": "Search again with different vocabulary, relax a preference the user agrees to "
    "(never a security gate), or build it.",
    "inconclusive": "Not a 'nothing exists' result: retry shortly (finished checks are cached). If GitHub "
    "is rate limited, wait or connect a token.",
}


class Decision(NamedTuple):
    verdict: Verdict
    build_yourself: bool  # recommended only on complete evidence that nothing fits
    reason: str
    next: str
    recommended: Scored | None = None


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


def _key(s: Scored) -> tuple[int, float, float]:
    # Fewest missing must-haves first, then the score; stars only break exact ties.
    return -len(s.missing), round(s.total, 3), math.log1p(s.repo.stars)


def select(scored: list[Scored], w: RankingSettings, rng: random.Random, k: int = 3) -> list[Scored]:
    passed = [s for s in scored if s.passed]
    established = sorted((s for s in passed if not (s.newer or s.probation)), key=_key, reverse=True)
    newer = [s for s in passed if s.newer or s.probation]
    picks = established[:k]
    if w.explore and newer:
        full = len(picks) == k
        floor = max(0.45, picks[-1].total * 0.85 if full else 0.45)
        eligible = [
            s
            for s in newer
            if s.total >= floor
            and s.relevance >= w.build_yourself_below
            # the explore slot never displaces a pick that covers more must-haves
            and (not full or len(s.missing) <= len(picks[-1].missing))
        ]
        if eligible:
            # The draw decides which newer project gets the slot, never how good the result is: only
            # the ones with the best coverage take part, so the verdict does not change run to run.
            fewest = min(len(s.missing) for s in eligible)
            eligible = [s for s in eligible if len(s.missing) == fewest]
            choice = max(eligible, key=lambda s: s.total * rng.betavariate(1 + s.pref_good, 1 + s.pref_bad))
            choice.explore = True
            picks = [*picks[: k - 1], choice] if full else [*picks, choice]
    return sorted(picks, key=_key, reverse=True)


def verdict(
    pool_size: int,
    picks: list[Scored],
    w: RankingSettings,
    *,
    must_have: int = 0,
    gaps: int = 0,
    unexamined: int = 0,
) -> Decision:
    """Decide one component. ``picks`` must be in ``select`` order.

    ``must_have`` is the number of must-have capabilities that were assessed, ``gaps`` the number of
    decisive checks that could not be completed, ``unexamined`` the number of candidates that passed
    the metadata gates but were never deep-checked.
    """
    top = next(
        (p for p in picks if p.relevance >= w.build_yourself_below and (not must_have or p.covered)), None
    )
    if top is not None:
        name = top.repo.full_name
        tail = "no gate check found a blocking issue."
        if gaps:
            tail += " Evidence is partial (see unresolved_checks): a better candidate may have been missed."
        if not must_have:
            reason = (
                f"{name} matches the task (relevance {top.relevance:.2f}); no must-have capabilities "
                f"were given, so coverage was not assessed; {tail}"
            )
            return Decision("reuse", False, reason, NEXT_STEP["reuse"], top)
        if not top.missing:
            reason = f"{name} shows evidence for all {must_have} must-have capabilities; {tail}"
            return Decision("reuse", False, reason, NEXT_STEP["reuse"], top)
        reason = (
            f"{name} shows evidence for {len(top.covered)} of {must_have} must-have capabilities "
            f"(not evidenced: {'; '.join(top.missing)}); {tail}"
        )
        return Decision("reuse_partial", False, reason, NEXT_STEP["reuse_partial"], top)

    if gaps:
        reason = (
            f"{gaps} decisive check{'s' if gaps != 1 else ''} could not be completed (see "
            "unresolved_checks), so neither a reuse nor a no-reuse conclusion is supported"
        )
        return Decision("inconclusive", False, reason, NEXT_STEP["inconclusive"])
    if not picks:
        if unexamined:
            reason = (
                f"every candidate examined in depth failed a gate, but {unexamined} more matched and "
                "were not examined (deep-check budget)"
            )
            step = "Call again with the rejected repositories in context.exclude to examine the rest."
            return Decision("inconclusive", False, reason, step)
        reason = (
            "no public repository matched the searches or the named candidates"
            if pool_size == 0
            else "every candidate failed a security or fit gate (see rejected)"
        )
        return Decision("no_candidates", True, reason, NEXT_STEP["no_candidates"])

    best = max(picks, key=lambda s: s.relevance)
    if best.relevance < w.build_yourself_below:
        reason = (
            f"closest match {best.repo.full_name} fits weakly (relevance {best.relevance:.2f}); "
            "a small custom implementation is likely cheaper than adapting it"
        )
    else:
        reason = (
            f"closest match {best.repo.full_name} shows no evidence for any of the {must_have} "
            "must-have capabilities"
        )
    return Decision("build_yourself", True, reason, NEXT_STEP["build_yourself"])
