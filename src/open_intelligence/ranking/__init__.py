"""Ranking: from each platform's raw results to an explained top 3 per need.

Pipeline (see docs/ranking-parameters.md): gather candidates with several queries and sorts,
apply gates, score what passes (fit, quality, health, trust, adoption), read the shortlist's
README or card and re-score, then pick a diverse top 3 with the evidence behind each pick.
"""

from open_intelligence.ranking.engine import Ranker, RankRequest
from open_intelligence.ranking.models import Constraints

__all__ = ["Constraints", "RankRequest", "Ranker"]
