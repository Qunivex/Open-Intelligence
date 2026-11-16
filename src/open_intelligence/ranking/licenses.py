"""Sort a license string, as GitHub, Hugging Face or Kaggle reports it, into a kind.

The three platforms spell licenses differently ("apache-2.0", "Apache 2.0", "CC BY-NC-SA 4.0",
"Data files © Original Authors"), so this works on patterns rather than an exact table. It is a
triage for ranking, not legal advice: every pick still shows the license text it was given.
"""

from __future__ import annotations

import re
from typing import Literal

LicenseKind = Literal[
    "public-domain",
    "permissive",
    "weak-copyleft",
    "copyleft",
    "use-restricted",
    "non-commercial",
    "unclear",
]

# How much trust a license kind earns (1 = no obstacle to building on it).
LICENSE_TRUST: dict[LicenseKind, float] = {
    "public-domain": 1.0,
    "permissive": 1.0,
    "weak-copyleft": 0.85,
    "copyleft": 0.7,
    "use-restricted": 0.55,
    "non-commercial": 0.45,
    # Unknown terms are neither good nor bad: neutral, and flagged on the pick. A harsher value (0.3)
    # pushed the datasets everyone uses below re-uploads with a tidier label (docs/evaluation.md).
    "unclear": 0.5,
}

_PATTERNS: tuple[tuple[LicenseKind, str], ...] = (
    # Not a grant at all, whatever else the string says.
    ("unclear", r"original authors|specified in description|^other$|^unknown$|noassertion|^none$|^$"),
    ("non-commercial", r"\bnc\b|non-?commercial"),
    ("use-restricted", r"\bnd\b|no-?deriv|rail|llama|gemma|research|terms of use|api terms|\beula\b"),
    ("public-domain", r"cc0|public domain|\bpddl\b|unlicense|government works|\bwtfpl\b"),
    ("copyleft", r"\bagpl|\bsspl|affero"),
    ("weak-copyleft", r"\blgpl|lesser general"),
    ("copyleft", r"\bgpl|general public license"),
    (
        "weak-copyleft",
        r"\bmpl\b|mozilla|\bepl\b|eclipse|\beupl|\bcddl|\bosl\b|\bgfdl|\bsa\b|share-?alike|\bodbl"
        r"|open database|cdla sharing|agreement sharing",
    ),
    (
        "permissive",
        r"\bmit\b|apache|bsd|\bisc\b|\bzlib\b|\bcc by\b|attribution|permissive|odc by|postgresql|\bbsl\b|boost"
        r"|\bafl\b|\becl\b|artistic|\bncsa\b|etalab",
    ),
)
_RULES: tuple[tuple[LicenseKind, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(pattern)) for kind, pattern in _PATTERNS
)


def normalize(raw: str | None) -> str:
    text = (raw or "").lower()
    text = text.replace("license:", " ")
    return re.sub(r"\s+", " ", re.sub(r"[-_/:,()]", " ", text)).strip()


def classify(raw: str | None) -> LicenseKind:
    text = normalize(raw)
    for kind, pattern in _RULES:
        if pattern.search(text):
            return kind
    return "unclear"


def allows_commercial_use(kind: LicenseKind) -> bool | None:
    """True / False, or None when the license does not settle it (restricted or unclear terms)."""
    if kind == "non-commercial":
        return False
    if kind in ("use-restricted", "unclear"):
        return None
    return True
