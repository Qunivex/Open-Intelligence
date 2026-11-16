"""Evaluation: does the ranking put better candidates first than each platform's own order?

A golden set lists real tasks and, for each, graded judgments of candidates. The runner ranks
every task, scores the top 3 against the judgments (nDCG@3, precision@3), does the same for
what each platform alone would have shown, and re-runs with each score part removed to see
which parts earn their weight. See docs/evaluation.md.
"""

from open_intelligence.evaluation.golden import Golden, GoldenTask, load_golden
from open_intelligence.evaluation.metrics import judged_at_k, ndcg_at_k, precision_at_k
from open_intelligence.evaluation.runner import Recorder, Report, evaluate, render_markdown

__all__ = [
    "Golden",
    "GoldenTask",
    "Recorder",
    "Report",
    "evaluate",
    "judged_at_k",
    "load_golden",
    "ndcg_at_k",
    "precision_at_k",
    "render_markdown",
]
