"""Search queries: sanitising the ones agents write, and a fallback when they write none.

Good discovery is the discovery skill's job: it turns intent into practitioner
vocabulary and a portfolio of differently-angled queries. This module only
makes those queries safe to send, and, when a caller sends no queries or
candidates at all, builds a modest fallback from the task's meaning-bearing
terms (generic request words such as "build", "help", "people" are dropped).
"""

from __future__ import annotations

import re

from reuse_layer.core.text import content_words, sanitize

_ALLOWED_QUALIFIERS = frozenset(
    {
        "in", "language", "topic", "topics", "stars", "forks", "pushed", "created", "license", "size",
        "archived", "fork", "is", "org", "user", "repo", "mirror", "template", "followers",
        "good-first-issues", "help-wanted-issues",
    }
)  # fmt: skip
_TOKEN = re.compile(r'-?[A-Za-z][\w.-]*:"[^"]*"|-?[A-Za-z][\w.-]*:\S+|"[^"]*"|\S+')
_OPERATORS = {"AND", "OR", "NOT"}
MAX_QUERY_CHARS = 240


def sanitize_query(raw: str) -> str | None:
    """A safe GitHub repository-search query, or None if nothing searchable is left."""
    q = sanitize(raw, 400)
    kept: list[str] = []
    operators = 0
    free_text = False
    has = {"is": False, "archived": False}
    for tok in _TOKEN.findall(q):
        if tok in _OPERATORS:
            operators += 1
            if operators <= 5 and kept:
                kept.append(tok)
            continue
        if ":" in tok and not tok.startswith('"'):
            key, _, value = tok.lstrip("-").partition(":")
            key = key.lower()
            if key not in _ALLOWED_QUALIFIERS or not value or "@me" in value:
                continue
            if key == "is" and value.lower() != "public":
                continue  # never ask for private or internal repositories
            if key in has:
                has[key] = True
            if key in ("topic", "topics", "language", "org", "user", "repo"):
                free_text = True
            kept.append(tok)
            continue
        if any(ch.isalnum() for ch in tok):
            free_text = True
            kept.append(tok)
    while kept and kept[-1] in _OPERATORS:
        kept.pop()
    if not free_text or not kept:
        return None
    if not has["is"]:
        kept.append("is:public")
    if not has["archived"]:
        kept.append("archived:false")
    out = " ".join(kept)
    if len(out) > MAX_QUERY_CHARS:
        out = out[:MAX_QUERY_CHARS].rsplit(" ", 1)[0]
    return out


def fallback_queries(task: str, capabilities: list[str], language: str | None, limit: int = 4) -> list[str]:
    """Queries from meaning-bearing terms when the caller supplied none (prefer the discovery skill)."""
    lang = f" language:{language}" if language and re.fullmatch(r"[a-z0-9+#.-]{1,30}", language) else ""
    raw: list[str] = []
    terms = content_words(task)
    if terms:
        raw.append(" ".join(terms[:3]))
        if len(terms) > 3:
            raw.append(" ".join(terms[1:4]))
    for cap in capabilities:
        first = content_words(cap.split("|", 1)[0])
        if first:
            raw.append(" ".join(first[:3]))
    out: list[str] = []
    for r in raw:
        q = sanitize_query(r + lang)
        if q and q not in out:
            out.append(q)
    return out[:limit]
