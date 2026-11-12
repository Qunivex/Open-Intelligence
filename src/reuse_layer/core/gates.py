"""Hard gates. Any failure removes the project from results; no model output can override one.

Gates fail closed: a check that could not complete (timeout, rate limit,
error) is a failure, reported as such, never a silent pass. Such a result
carries ``complete=False`` so that "could not be checked" stays distinguishable
from "was checked and failed": both keep the project out of the picks, but
only the second is evidence against it.

Unknown data fails closed too. A high or critical advisory that names no
fixed version fails the ``vulns`` gate whether the advisory says "no fix" or
says nothing: the check completed and found an advisory it cannot clear, so
that is a finding (``complete=True``), not an unchecked candidate.

Metadata gates run first on every candidate (cheap: status, license, age,
language). Evidence gates run on the shortlist after enrichment
(vulnerabilities, malware, pinning and provenance, lookalike names).

A passed gate means that check found no blocking issue. It is not a statement
that the project is safe or compatible: the gates do not read source code,
scan dependencies or run anything. ``unresolved_checks`` lists what could not
be established even for a project that passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from reuse_layer.core import licenses
from reuse_layer.core.models import GateResult, Repo, Scored

_LANG_GROUPS = (
    {"javascript", "typescript", "js", "ts", "node", "nodejs", "vue", "svelte"},
    {"python", "py", "jupyter notebook", "cython"},
    {"c++", "cpp", "c"},
    {"c#", "csharp", "f#", ".net", "dotnet"},
    {"go", "golang"},
    {"shell", "bash", "zsh", "powershell"},
    {"java", "kotlin", "scala", "groovy"},
)

# GitHub verification reasons that mean a signature exists and is wrong.
_BAD_SIGNATURES = frozenset(
    {"invalid", "malformed_signature", "malformed_sig", "bad_cert", "not_signing_key"}
)


@dataclass(frozen=True)
class GatePolicy:
    license_policy: str | tuple[str, ...]
    min_age_days: int
    max_days_since_push: int
    allow_forks: bool
    allow_new: bool
    require_signed_releases: bool
    language: str | None


def same_language(want: str, have: str) -> bool:
    w, h = want.lower(), have.lower()
    if w == h:
        return True
    return any(w in g and h in g for g in _LANG_GROUPS)


def metadata_gates(repo: Repo, p: GatePolicy, now: datetime) -> list[GateResult]:
    out: list[GateResult] = []

    if repo.disabled:
        out.append(GateResult("status", False, "disabled by GitHub"))
    elif repo.archived:
        out.append(GateResult("status", False, "archived: read-only and unmaintained"))
    elif repo.fork and not p.allow_forks:
        out.append(GateResult("status", False, "a fork; prefer the upstream project"))
    else:
        d = repo.days_since_push(now)
        if d is not None and d > p.max_days_since_push:
            out.append(GateResult("status", False, f"no push in {d} days: unmaintained"))
        else:
            out.append(GateResult("status", True, "active"))

    ok, why = licenses.allowed(repo.license_spdx, p.license_policy)
    out.append(GateResult("license", ok, why))

    age = repo.age_days(now)
    if age < p.min_age_days and not p.allow_new:
        out.append(
            GateResult(
                "age", False, f"created {age} days ago (minimum {p.min_age_days}; pass allow_new to include)"
            )
        )
    else:
        out.append(GateResult("age", True, f"{age} days"))

    if p.language and repo.language and not same_language(p.language, repo.language):
        out.append(GateResult("fit", False, f"written in {repo.language}; your project uses {p.language}"))
    else:
        out.append(GateResult("fit", True, repo.language or "no primary language (docs or data)"))
    return out


def evidence_gates(s: Scored, p: GatePolicy, lookalike_of: str | None, now: datetime) -> list[GateResult]:
    deep = s.deep
    out: list[GateResult] = []
    if deep is None:
        why = s.deep_error or "time budget or rate limit"
        return [
            GateResult(
                "evidence", False, f"evidence checks did not run ({why}); fails closed", complete=False
            )
        ]

    live = [a for a in deep.advisories if not a.malware]
    severe = [a for a in live if a.severity in ("high", "critical")]
    blocking = [a for a in severe if a.patched is False]
    unconfirmed = [a for a in severe if a.patched is None]
    # A finding from any source that answered is a real failure, even if another source did not.
    if blocking:
        a = blocking[0]
        out.append(
            GateResult("vulns", False, f"{a.ghsa_id} ({a.severity}) has no fixed version: {a.summary}")
        )
    elif unconfirmed:
        # The advisory was read and names no fixed version. "No fix on record" is not cleared by
        # assuming one exists: for high and critical severity it fails closed, as a finding.
        a = unconfirmed[0]
        out.append(
            GateResult(
                "vulns",
                False,
                f"{a.ghsa_id} ({a.severity}) lists no fixed version, fix status unknown "
                f"(fails closed): {a.summary}",
            )
        )
    elif not deep.advisories_checked:
        out.append(
            GateResult(
                "vulns", False, "vulnerability check could not complete (fails closed)", complete=False
            )
        )
    elif live:
        unfixed = [a for a in live if a.patched is False]
        unknown = [a for a in live if a.patched is None]
        parts = [f"{len(live)} published advisories"]
        if unfixed:
            parts.append(f"{len(unfixed)} low/medium without a fix")
            s.risks.append(f"{len(unfixed)} low/medium advisories without a fixed version")
        if unknown:  # below high severity this is a risk, and it is never reported as fixed
            parts.append(f"fix status unknown for {len(unknown)}")
            s.risks.append(f"Fix status unknown for {len(unknown)} low/medium advisories: review them")
        if not unfixed and not unknown:
            parts.append("all with fixes")
        out.append(GateResult("vulns", True, ", ".join(parts)))
    else:
        out.append(GateResult("vulns", True, "no published advisories"))

    malware = [a for a in deep.advisories if a.malware]
    out.append(
        GateResult(
            "malware",
            not malware,
            f"malware advisory {malware[0].ghsa_id}" if malware else "no malware flags",
        )
    )

    pin = deep.pin
    if pin is None:
        out.append(
            GateResult(
                "provenance", False, "could not resolve a commit to pin (fails closed)", complete=False
            )
        )
    elif pin.signature in _BAD_SIGNATURES:
        out.append(
            GateResult("provenance", False, f"signature on {pin.ref} failed verification ({pin.signature})")
        )
    elif p.require_signed_releases and pin.signature != "valid":
        out.append(
            GateResult("provenance", False, f"{pin.ref} is not signed and your policy requires signatures")
        )
    else:
        label = "signed, verified by GitHub" if pin.signature == "valid" else f"not signed ({pin.signature})"
        out.append(GateResult("provenance", True, f"pinned {pin.kind} {pin.ref} @ {pin.sha[:12]}, {label}"))

    if lookalike_of is not None and (s.repo.age_days(now) < 365 or (deep.contributors or 0) < 3):
        out.append(GateResult("lookalike", False, f"name imitates the more established {lookalike_of}"))
    elif lookalike_of is not None:
        out.append(
            GateResult("lookalike", True, f"name close to {lookalike_of}; check it is the one you want")
        )
        s.risks.append(f"Name close to {lookalike_of}")
    return out


# Evidence fetches that can fail without failing a gate, and what their absence leaves unknown.
_UNRESOLVED = {
    "readme": "README not read: capability coverage is judged from repository metadata only",
    "releases": "releases not listed: the pin may not be the latest release",
    "tags": "tags not listed: the pin may not be the latest version",
    "contributors": "contributor data not read",
    "community": "community health not read",
    "org": "organization verification not read",
    "checks": "upstream CI status not read",
    "check runs": "upstream CI has more check runs than were read, so it is not reported as passing",
    "pin": "release tag could not be resolved to a commit",
}


def unresolved_checks(s: Scored) -> list[str]:
    """What stayed unknown about a project that passed its gates. Never a reason to call it checked."""
    deep = s.deep
    if deep is None:
        return []
    failed = sorted({e.split(":", 1)[0] for e in deep.errors})
    out = [_UNRESOLVED.get(name, f"{name} check did not complete") for name in failed]
    if deep.advisories_unattributed:
        out.append(
            f"{deep.advisories_unattributed} advisories for a package with this name cite no repository: "
            "not counted against it, review them"
        )
    if deep.ci == "pending":
        out.append("upstream CI still running on the pinned commit")
    elif deep.ci == "unknown" and not {"checks", "check runs"} & set(failed):
        out.append("upstream CI status unknown")
    if deep.pin is not None and deep.pin.signature == "unknown":
        out.append("commit signature could not be read")
    return out
