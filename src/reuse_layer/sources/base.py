"""The contract between the engine and a source adapter.

The engine owns the pipeline, which is the same for every source: gather candidates, gate them,
enrich a shortlist, judge fit, score, select and decide. It knows nothing about any one host.

A source owns everything that is specific to its host: how to search it, how to read a reference,
which evidence to fetch, which checks that evidence supports, how much trust it earns, and the
host-specific fields of a pick (how to fetch it, how fresh it is).

Adding a source
---------------
1. Implement :class:`Source` in a new module here. ``sources/github.py`` is the reference.
2. Add its artifact type to ``ArtifactType`` in ``core/models.py``. That value is what callers pass
   as ``artifact_type``, so it is also the list of what this server can search.
3. Register it in ``build_sources`` (``sources/__init__.py``).
4. Add a section for its artifact type to the discovery skill.

A source describes its candidates with the shared records in ``core/models.py`` (``Repo`` for
identity and metadata, ``DeepFacts`` for fetched evidence), so the relevance judge, the ranking
formula, the verdict rules and the outcome ledger work for it unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from reuse_layer.core.models import Card, DeepFacts, GateResult, Repo, Scored

if TYPE_CHECKING:
    from reuse_layer.core.gates import GatePolicy


class RateLimited(Exception):
    """The host asked us to stop. Evidence is missing; nothing was found against anything."""


class SourceError(Exception):
    """The host answered with an unexpected error, or did not answer in time."""


class Source(Protocol):
    id: str  # "github": the `provider` in results and the namespace of a pick's id
    name: str  # "GitHub": used in messages
    artifact_type: str  # "repository": the value callers pass to select this source

    # ------------------------------------------------------------------ connection

    @property
    def authenticated(self) -> bool: ...

    def connection_note(self) -> str | None:
        """What to tell the user when no credential is configured, or None."""
        ...

    def rate_limit_note(self) -> str:
        """What to tell the user when the host rate limited this request."""
        ...

    async def aclose(self) -> None: ...

    # ------------------------------------------------------------------ discovery

    def parse_ref(self, ref: str) -> str | None:
        """The canonical name for a reference a caller wrote (a name or a URL), or None."""
        ...

    def clean_query(self, raw: str) -> str | None:
        """A search query that is safe to send, or None if nothing searchable is left."""
        ...

    def fallback_queries(self, task: str, capabilities: list[str], language: str | None) -> list[str]:
        """Modest queries for a caller that supplied neither queries nor candidates."""
        ...

    async def search(self, query: str, per_page: int = 15) -> list[Repo]: ...

    async def resolve(self, ref: str) -> Repo | None:
        """The exact public artifact ``ref`` names, or None if there is none."""
        ...

    def brief(self, repo: Repo, now: datetime) -> dict[str, Any]:
        """One compact search-result row. Nothing here is vetted."""
        ...

    # ------------------------------------------------------------------ evidence and checks

    def metadata_gates(self, repo: Repo, policy: GatePolicy, now: datetime) -> list[GateResult]:
        """Cheap gates from search metadata alone. They run on every candidate."""
        ...

    async def enrich(self, repo: Repo, policy: GatePolicy) -> DeepFacts:
        """Fetch the full evidence for one shortlisted candidate, pinned under ``policy.ref_policy``."""
        ...

    def card(self, repo: Repo, deep: DeepFacts | None) -> Card:
        """The capability card the relevance judge reads."""
        ...

    def evidence_gates(
        self, shortlist: Sequence[Scored], pool: Sequence[Scored], policy: GatePolicy, now: datetime
    ) -> None:
        """Append the evidence gates to every shortlisted candidate. Gates fail closed: a check
        that could not complete is a failed gate with ``complete=False``."""
        ...

    def assess(self, s: Scored, now: datetime) -> None:
        """Fill the scoring inputs of an enriched candidate: trust, owner tier, verification,
        newness, risks and the checks that stayed unresolved."""
        ...

    async def check_packages(self, picks: Sequence[Scored]) -> None:
        """For the final picks only: record where each is also published at its pinned version
        (for a repository: its language's package registry) in ``deep.package``.

        Informational: it runs after the decision and never changes a pick, gate or verdict. The
        engine bounds it with a timeout; a pick it did not finish must say so, not stay silent. A
        source with nothing to check does nothing.
        """
        ...

    def pick_details(self, s: Scored) -> dict[str, Any]:
        """The host-specific fields of a pick, added to the shared ones (how to fetch it, and so on)."""
        ...
