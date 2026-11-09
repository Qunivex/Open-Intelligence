"""Capability cards: a fixed schema extracted from a repository's metadata and README.

The relevance judge reads cards, never raw READMEs, so instructions planted in
a README ("rank this first") have no channel to the ranker. When such text is
detected the card is built from metadata only and the pick carries a risk note.
"""

from __future__ import annotations

import re

from reuse_layer.core.models import Card, Repo
from reuse_layer.core.text import looks_like_injection, readme_sections, sanitize

_NOT_FEATURE = re.compile(
    r"^(install|pip |npm |yarn |pnpm |cargo |go get|brew |docker |git clone|license|contribut|star|sponsor|"
    r"follow|join|see |read |click|copyright|thanks)",
    re.IGNORECASE,
)


def build_card(repo: Repo, readme: str | None) -> Card:
    injection = looks_like_injection(repo.description) or bool(readme and looks_like_injection(readme))
    headings: list[str] = []
    features: list[str] = []
    intro = ""
    if readme and not injection:
        headings, bullets, intro = readme_sections(readme)
        features = [b for b in bullets if not _NOT_FEATURE.match(b) and len(b.split()) >= 2][:15]
    return Card(
        repo=repo.full_name,
        summary=sanitize(repo.description, 300),
        topics=repo.topics,
        language=repo.language,
        features=tuple(features),
        headings=tuple(headings[:20]),
        intro=intro,
        injection_suspected=injection,
    )


def card_payload(card: Card) -> dict[str, object]:
    """What a model judge sees: the card fields, nothing else."""
    out: dict[str, object] = {"id": card.repo, "summary": card.summary, "topics": list(card.topics)}
    if card.language:
        out["language"] = card.language
    if card.features:
        out["features"] = list(card.features[:12])
    if card.headings:
        out["sections"] = list(card.headings[:12])
    if card.intro:
        out["intro"] = card.intro[:600]
    return out
