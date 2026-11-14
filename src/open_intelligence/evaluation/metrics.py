"""Ranking metrics over graded judgments.

Grades run from 0 (poor or off-task) to 3 (the standard choice). A candidate with no judgment
counts as 0, which is pessimistic; ``judged_at_k`` says how much of a top-k was actually judged,
so a low score can be told apart from an unjudged one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import log2

GOOD = 2  # the grade from which a candidate counts as a good pick


def _dcg(grades: Sequence[int]) -> float:
    return float(sum((2.0**grade - 1.0) / log2(position + 2) for position, grade in enumerate(grades)))


def ndcg_at_k(ranked: Sequence[str], judgments: Mapping[str, int], k: int = 3) -> float | None:
    """How close the top k is to the best possible top k (1.0 = ideal). None when nothing is worth finding."""
    ideal = sorted(judgments.values(), reverse=True)[:k]
    if not ideal or ideal[0] <= 0:
        return None
    got = [judgments.get(key, 0) for key in ranked[:k]]
    return _dcg(got) / _dcg(ideal)


def precision_at_k(ranked: Sequence[str], judgments: Mapping[str, int], k: int = 3) -> float:
    """Share of the top k slots filled with a good pick. An empty slot counts as a miss."""
    return sum(judgments.get(key, 0) >= GOOD for key in ranked[:k]) / k


def judged_at_k(ranked: Sequence[str], judgments: Mapping[str, int], k: int = 3) -> float:
    """Share of the returned top k that has a judgment at all."""
    top = ranked[:k]
    return sum(key in judgments for key in top) / len(top) if top else 0.0
