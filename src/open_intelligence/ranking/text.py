"""Lexical matching for the fit score: explainable, no model, no network.

Limits worth knowing: this matches words, not meaning. "reverse ETL" does not match "warehouse
activation". The caller makes up for it by passing practitioner vocabulary and several phrasings.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_WORD = re.compile(r"[a-z0-9][a-z0-9+#]*(?:\.[a-z0-9]+)*")
_STOP = frozenset(
    "a an the and or of for to in on with by from as at is are be this that it its into over using use via "
    "your our my i we you can will should not no need want find best good open source based".split()
)


def _singular(word: str) -> str:
    if len(word) > 3 and word.isalpha() and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def words(text: str) -> list[str]:
    """Lower-cased content words; '-', '_' and '/' split words, 'node.js' and 'c++' stay whole."""
    text = text.lower().replace("_", " ").replace("-", " ").replace("/", " ").replace(":", " ")
    out = []
    for raw in _WORD.findall(text):
        word = _singular(raw.strip("."))
        if word and word not in _STOP:
            out.append(word)
    return out


def same_word(a: str, b: str) -> bool:
    """Equal, or close inflections of one word: classify/classifier/classification, train/training."""
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 5 or len(long) - len(short) > 7:
        return False
    if long.startswith(short):
        return True
    common = 0
    for x, y in zip(short, long, strict=False):
        if x != y:
            break
        common += 1
    # "classify" / "classification", "generate" / "generation": all but the last letter or two agree.
    return len(short) >= 7 and common >= len(short) - (2 if len(short) >= 8 else 1)


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class Vocabulary:
    """The words of one piece of text, with inflection-tolerant lookup.

    Names written in CamelCase are kept whole and also split, so "CustomerChurnTelecom" matches
    "customer churn" and "PyTorch" still matches "pytorch".
    """

    def __init__(self, texts: Iterable[str]) -> None:
        self._words: set[str] = set()
        for text in texts:
            self._words.update(words(text))
            self._words.update(words(_CAMEL.sub(" ", text)))
        self._long = [w for w in self._words if len(w) >= 5]

    def __contains__(self, word: object) -> bool:
        if not isinstance(word, str):
            return False
        if word in self._words:
            return True
        return len(word) >= 5 and any(same_word(word, other) for other in self._long)

    def __len__(self) -> int:
        return len(self._words)


def coverage(phrase: str, strong: Vocabulary, weak: Vocabulary, weak_credit: float = 0.7) -> float:
    """Share of the phrase's words found: full credit in name/tags/description, less in the long text."""
    wanted = list(dict.fromkeys(words(phrase)))
    if not wanted:
        return 0.0
    credit = 0.0
    for word in wanted:
        if word in strong:
            credit += 1.0
        elif word in weak:
            credit += weak_credit
    return credit / len(wanted)


def jaccard(a: str, b: str) -> float:
    wa, wb = set(words(a)), set(words(b))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)
