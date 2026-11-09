"""License policy: SPDX ids as GitHub's license detection reports them.

A repository with no license, or one GitHub cannot identify (``NOASSERTION``),
is not open source for reuse purposes (all rights reserved) and fails the gate.
"""

from __future__ import annotations

PERMISSIVE = frozenset(
    {
        "MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "BSD-3-Clause-Clear", "ISC", "0BSD",
        "Unlicense", "Zlib", "BSL-1.0", "PSF-2.0", "Python-2.0", "CC0-1.0", "UPL-1.0", "NCSA", "X11",
        "PostgreSQL", "BlueOak-1.0.0", "MulanPSL-2.0", "AFL-3.0", "ECL-2.0", "MS-PL", "CC-BY-4.0",
    }
)  # fmt: skip
WEAK_COPYLEFT = frozenset(
    {
        "LGPL-2.1", "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0", "LGPL-3.0-only", "LGPL-3.0-or-later",
        "MPL-2.0", "EPL-1.0", "EPL-2.0", "CDDL-1.0", "CDDL-1.1", "Artistic-2.0", "MS-RL", "OFL-1.1",
    }
)  # fmt: skip
STRONG_COPYLEFT = frozenset(
    {
        "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later",
        "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later", "EUPL-1.1", "EUPL-1.2", "OSL-3.0", "CECILL-2.1",
        "CC-BY-SA-4.0", "LPPL-1.3c",
    }
)  # fmt: skip

POLICIES: dict[str, frozenset[str]] = {
    "permissive": PERMISSIVE,
    "weak_copyleft": PERMISSIVE | WEAK_COPYLEFT,
    "any_osi": PERMISSIVE | WEAK_COPYLEFT | STRONG_COPYLEFT,
}

_LOWER = {s.lower(): s for s in PERMISSIVE | WEAK_COPYLEFT | STRONG_COPYLEFT}


def canonical(spdx: str | None) -> str | None:
    if not spdx:
        return None
    return _LOWER.get(spdx.strip().lower(), spdx.strip())


def family(spdx: str | None) -> str:
    s = canonical(spdx)
    if s is None or s == "NOASSERTION":
        return "unknown"
    if s in PERMISSIVE:
        return "permissive"
    if s in WEAK_COPYLEFT:
        return "weak copyleft"
    if s in STRONG_COPYLEFT:
        return "strong copyleft"
    return "other"


def allowed(spdx: str | None, policy: str | tuple[str, ...]) -> tuple[bool, str]:
    """(passes, human reason) for ``spdx`` under ``policy``."""
    s = canonical(spdx)
    if s is None:
        return False, "no license: all rights reserved, cannot be reused"
    if s == "NOASSERTION":
        return False, "license not recognised by GitHub; review it by hand before reuse"
    if isinstance(policy, tuple):
        allowed_ids = {canonical(p) for p in policy}
        ok = s in allowed_ids
        return ok, f"{s}, {'in' if ok else 'not in'} your allowed list"
    pool = POLICIES.get(policy, PERMISSIVE)
    if s in pool:
        return True, f"{s}, compatible with {policy.replace('_', ' ')} policy"
    return False, f"{s} ({family(s)}) not allowed by {policy.replace('_', ' ')} policy"
