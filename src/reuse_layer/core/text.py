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
# Every pattern is an instruction or an address to the reader. Vocabulary is not: "tool call" and
# "system prompt" are what LLM projects are about, and matching them threw away the README of
# exactly the projects this layer is most often asked about.
_INJECTION = re.compile(
    r"(ignore (all |any )?(previous|prior|above) (instructions|prompts)|disregard (the|all|previous) instructions|"
    r"you are (an? )?(ai|assistant|language model|llm)|rank (this|me|us) (first|highest|#?1)|"
    r"recommend (this|our) (repo|project|package) (above|over|first)|<\s*/?\s*(system|assistant|instructions?)\s*>|"
    r"as an ai (agent|assistant)|do not tell the user)",
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
    """True when text outside code blocks addresses or instructs an AI reader.

    Fenced code is left out: it is where LLM projects show example prompts ("You are an AI
    assistant..."), and it never reaches a capability card anyway. HTML comments are kept in, because
    a comment is where a README would hide an instruction from human readers.
    """
    return bool(_INJECTION.search(_FENCE.sub(" ", text)))


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
# A title with a line of ===, ---, ~~~ or ^^^ under it: how reStructuredText READMEs (common for
# Python projects) write every heading, and Markdown's other heading style.
_UNDERLINED = re.compile(
    r"^[ \t]*([^\s#|>*+\-][^\n]{1,79}?)[ \t]*\n[ \t]*(?:={3,}|-{3,}|~{3,}|\^{3,})[ \t]*$", re.MULTILINE
)
_RULE = re.compile(r"[=\-~^]{3,}")
_BULLET = re.compile(r"^\s{0,6}(?:[-*+]|\d+[.)])\s+(.+)$", re.MULTILINE)
# Inline emphasis only: the markers hug the text and stay on one line. A looser pattern pairs the "*"
# of one bullet with the "*" of the next and deletes every bullet in a "*"-style list.
_EMPH = re.compile(r"(?<![\w*_])([*_]{1,3})(?=\S)([^*_\n]+?)(?<=\S)\1(?![\w*_])")


def strip_markdown_noise(md: str) -> str:
    md = _HTML_COMMENT.sub(" ", md)
    md = _FENCE.sub(" ", md)
    md = _IMAGE.sub(" ", md)
    md = _LINK.sub(r"\1", md)
    md = _REF_LINK.sub(" ", md)
    md = _HTML_TAG.sub(" ", md)
    md = _BARE_URL.sub(" ", md)
    md = _INLINE_CODE.sub(r"\1", md)
    md = _EMPH.sub(r"\2", md)
    return md


def readme_sections(md: str) -> tuple[list[str], list[str], str]:
    """Split a README into (headings, bullet items, intro prose). All sanitised."""
    clean = strip_markdown_noise(md)
    headings = [sanitize(h, 80) for h in _HEADING.findall(clean) + _UNDERLINED.findall(clean)]
    bullets = [sanitize(b, 140) for b in _BULLET.findall(clean)]
    prose_lines = []
    for line in clean.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "|", ">")) or _BULLET.match(line) or _RULE.fullmatch(s):
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
_CAMEL_WORD = re.compile(r"[A-Za-z0-9]*[a-z0-9][A-Z][A-Za-z0-9]*")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#]*")

# Equivalences the inflection rules below cannot reach: synonyms, abbreviations and a few derived
# forms. Keys and values are stems as `_inflect` produces them, so every form of a word lands on
# the same entry ("tokenize", "tokenized" and "tokenizing" are all the key "tokeniz").
_CANON = {
    "apis": "api",
    "genai": "llm",
    "gpt": "llm",
    "tokeniz": "token",
    "tokenizer": "token",
    "tokenization": "token",
    "tokenis": "token",
    "tokeniser": "token",
    "tokenisation": "token",
    "estimator": "estimat",
    "estimation": "estimat",
    "calculator": "calculat",
    "calculation": "calculat",
    "auth": "authentication",
    "authn": "authentication",
    "authenticat": "authentication",
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
}

# Words whose ending only looks like an inflection.
_NOT_INFLECTED = frozenset({"embed"})


def _consonant(w: str, i: int) -> bool:
    if w[i] in "aeiou":
        return False
    return w[i] != "y" or i == 0 or not _consonant(w, i - 1)


def _measure(w: str) -> int:
    """Porter's m: the number of vowel-then-consonant runs in ``w``."""
    m, in_vowels = 0, False
    for i in range(len(w)):
        if _consonant(w, i):
            m += in_vowels
            in_vowels = False
        else:
            in_vowels = True
    return m


def _has_vowel(w: str) -> bool:
    return any(not _consonant(w, i) for i in range(len(w)))


def _cvc(w: str) -> bool:
    """Ends consonant-vowel-consonant (the last not w, x or y): a short stem whose final e matters."""
    return (
        len(w) >= 3
        and _consonant(w, len(w) - 3)
        and not _consonant(w, len(w) - 2)
        and _consonant(w, len(w) - 1)
        and w[-1] not in "wxy"
    )


def _inflect(w: str) -> str:
    """The inflectional steps of Porter's stemmer (1a, 1b, 1c and 5a), and nothing more.

    "caching", "cached", "caches" and "cache" must meet, as must "logging" and "log", so plurals,
    -ing, -ed and a silent final e are folded. Porter's measure keeps short words apart: "pipe" does
    not become "pip", nor "note" "not". The derivational steps are left out on purpose: folding
    "generate", "general" and "generic" together would report capabilities as covered that are not.
    """
    if len(w) <= 3 or w in _NOT_INFLECTED:
        return w
    # 1a: plurals
    if (w.endswith("ies") and len(w) > 4) or w.endswith(("ches", "shes", "xes", "zes", "sses")):
        w = w[:-2]  # "queries" -> "queri", "caches" -> "cach", "classes" -> "class"
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    # 1b: -eed, -ed, -ing
    if w.endswith("eed"):
        if _measure(w[:-3]) > 0:
            w = w[:-1]
    elif w not in _NOT_INFLECTED:
        for suffix in ("ing", "ed"):
            base = w[: -len(suffix)]
            if w.endswith(suffix) and _has_vowel(base):
                if len(base) >= 4 and base[-1] == base[-2] and _consonant(base, len(base) - 1):
                    if base[-1] not in "lsz":
                        base = base[:-1]  # "logging" -> "log"; "billing" and "passing" keep theirs
                elif _measure(base) == 1 and _cvc(base):
                    base += "e"  # "pricing" -> "price", "typed" -> "type"
                w = base
                break
    # 1c: a final y after a vowel-bearing stem
    if len(w) >= 4 and w.endswith("y") and _has_vowel(w[:-1]):
        w = w[:-1] + "i"
    # 5a: a silent final e
    if len(w) >= 4 and w.endswith("e"):
        m = _measure(w[:-1])
        if m > 1 or (m == 1 and not _cvc(w[:-1])):
            w = w[:-1]
    return w


def stem(word: str) -> str:
    s = _inflect(word)
    return _CANON.get(s, s)


def tokens(text: str, *, drop_generic: bool = False, query: bool = False) -> list[str]:
    """Lower-cased, stemmed content tokens, in order (duplicates kept).

    A camel-cased word is one word and several. In repository text it yields both ("WebSocket" gives
    websocket, web, socket), so it matches however the capability is phrased. In a ``query`` (a
    capability, task or avoid phrase) it yields only the joined form, so "PostgreSQL" asks for
    postgresql and not for anything that mentions "sql".
    """

    def camel(m: re.Match[str]) -> str:
        word = m.group(0)
        return word.lower() if query else f"{word.lower()} {_SPLIT_CAMEL.sub(' ', word)}"

    text = _CAMEL_WORD.sub(camel, text)
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
