"""Text handling for untrusted content (descriptions, topics, READMEs) and for matching.

Everything fetched from GitHub is data, not instructions. It is cleaned here
(control, zero-width and bidi characters removed, length capped) before it is
put into a capability card or a response.
"""

from __future__ import annotations

import re
import unicodedata

# Bidi overrides, zero-width characters and other invisible format characters.
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿­]")
_WS = re.compile(r"\s+")

# Phrases that try to steer a model that reads the text. Flagged, never obeyed.
_INJECTION = re.compile(
    r"(ignore (all |any )?(previous|prior|above) (instructions|prompts)|disregard (the|all|previous) instructions|"
    r"you are (an? )?(ai|assistant|language model|llm)|system prompt|rank (this|me|us) (first|highest|#?1)|"
    r"recommend (this|our) (repo|project|package) (above|over|first)|<\s*/?\s*(system|assistant|instructions?)\s*>|"
    r"as an ai (agent|assistant)|tool[_ ]?call|do not tell the user)",
    re.IGNORECASE,
)


def sanitize(text: object, limit: int = 300) -> str:
    """Return printable single-line text, at most ``limit`` characters."""
    if not isinstance(text, str):
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _INVISIBLE.sub("", t)
    t = "".join(ch if ch.isprintable() or ch in "\n\t" else " " for ch in t)
    t = _WS.sub(" ", t).strip()
    if len(t) > limit:
        t = t[: max(0, limit - 1)].rstrip() + "…"
    return t


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION.search(text))


# --------------------------------------------------------------------------- markdown → plain text

_FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]{0,500}>")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_REF_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.MULTILINE)
_BARE_URL = re.compile(r"https?://\S+")
_INLINE_CODE = re.compile(r"`([^`]{0,80})`")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_BULLET = re.compile(r"^\s{0,6}(?:[-*+]|\d+[.)])\s+(.+)$", re.MULTILINE)
_EMPH = re.compile(r"[*_]{1,3}([^*_]+)[*_]{1,3}")


def strip_markdown_noise(md: str) -> str:
    md = _HTML_COMMENT.sub(" ", md)
    md = _FENCE.sub(" ", md)
    md = _IMAGE.sub(" ", md)
    md = _LINK.sub(r"\1", md)
    md = _REF_LINK.sub(" ", md)
    md = _HTML_TAG.sub(" ", md)
    md = _BARE_URL.sub(" ", md)
    md = _INLINE_CODE.sub(r"\1", md)
    md = _EMPH.sub(r"\1", md)
    return md


def readme_sections(md: str) -> tuple[list[str], list[str], str]:
    """Split a README into (headings, bullet items, intro prose). All sanitised."""
    clean = strip_markdown_noise(md)
    headings = [sanitize(h, 80) for h in _HEADING.findall(clean)]
    bullets = [sanitize(b, 140) for b in _BULLET.findall(clean)]
    prose_lines = []
    for line in clean.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "|", ">")) or _BULLET.match(line):
            continue
        prose_lines.append(s)
        if sum(len(p) for p in prose_lines) > 1500:
            break
    intro = sanitize(" ".join(prose_lines), 1200)
    return [h for h in headings if h][:25], [b for b in bullets if len(b) > 3][:40], intro


# --------------------------------------------------------------------------- matching

STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have how i if in into is it its
    me my of on or our out over so some such than that the their them then there these they this those to too
    up us was we were what when where which while who why will with would you your yours via per vs etc also
    just any all each every more most other own same only very s t across between among within without about
    around against""".split()
)

# Words that describe the request rather than the capability. Dropped when
# turning a task into search terms (the reason keyword splitting fails).
GENERIC = frozenset(
    """want wants wanted need needs needed like would build building built create creating make making help helps
    helping people person user users understand understanding allow allows allowing let lets enable enables app
    apps application applications tool tools thing things something way ways using use used uses different various
    implementation implement implementing based simple easy easily good better best new able provide provides
    providing support supports supporting feature features project projects solution solutions platform system
    systems service services looking find finding get getting give gives work works working could should might
    kind type types lot lots many much really actually basically""".split()
)

_SPLIT_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#]*")

# A few equivalences that matter for matching capability text to repo text.
_CANON = {
    "llms": "llm",
    "large-language-model": "llm",
    "genai": "llm",
    "gpt": "llm",
    "prices": "price",
    "pricing": "price",
    "priced": "price",
    "costs": "cost",
    "costing": "cost",
    "tokens": "token",
    "tokenizer": "token",
    "tokenizers": "token",
    "tokenization": "token",
    "tokenize": "token",
    "estimator": "estimate",
    "estimation": "estimate",
    "estimating": "estimate",
    "estimates": "estimate",
    "calculator": "calculate",
    "calculation": "calculate",
    "calculations": "calculate",
    "calculating": "calculate",
    "providers": "provider",
    "auth": "authentication",
    "authn": "authentication",
    "pdf": "pdf",
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
}


def stem(word: str) -> str:
    w = _CANON.get(word, word)
    if w != word or len(w) <= 4:
        return w
    for suffix, repl in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            if suffix == "es" and not w.endswith(("ches", "shes", "xes", "zes", "sses")):
                continue
            if suffix == "s" and w.endswith(("ss", "us", "is")):
                continue
            return w[: len(w) - len(suffix)] + repl
    return w


def tokens(text: str, *, drop_generic: bool = False) -> list[str]:
    """Lower-cased, stemmed content tokens, in order (duplicates kept)."""
    text = _SPLIT_CAMEL.sub(" ", text)
    text = text.replace("-", " ").replace("_", " ").replace("/", " ").lower()
    out = []
    for raw in _TOKEN.findall(text):
        if raw in STOPWORDS or (drop_generic and raw in GENERIC):
            continue
        if raw.isdigit() and len(raw) < 3:
            continue
        out.append(stem(raw))
    return out


def content_words(text: str) -> list[str]:
    """Unstemmed, lower-cased meaning-bearing words (for search queries), unique, in order."""
    words = []
    for raw in _TOKEN.findall(text.lower()):
        if raw in STOPWORDS or raw in GENERIC or (raw.isdigit() and len(raw) < 3) or len(raw) < 2:
            continue
        words.append(raw)
    return unique(words)


def unique(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out
