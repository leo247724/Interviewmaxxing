"""Per-source adapters. Each reads the source's visible UI through a BrowserTransport."""

from __future__ import annotations

from .base import AccessProblem, Observation, SearchContext, SearchLeg, SourceAdapter, SourceOutcome
from .builtin import BuiltInAdapter
from .google import GoogleAdapter
from .indeed import IndeedAdapter
from .linkedin import LinkedInAdapter


def default_adapters() -> dict[str, SourceAdapter]:
    """The four requested sources, keyed by D0 source slug."""
    adapters: list[SourceAdapter] = [LinkedInAdapter(), BuiltInAdapter(), IndeedAdapter(),
                                     GoogleAdapter()]
    return {a.name: a for a in adapters}


__all__ = [
    "AccessProblem",
    "BuiltInAdapter",
    "GoogleAdapter",
    "IndeedAdapter",
    "LinkedInAdapter",
    "Observation",
    "SearchContext",
    "SearchLeg",
    "SourceAdapter",
    "SourceOutcome",
    "default_adapters",
]
