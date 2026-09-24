"""Explicit opt-in semantic application routing; never controls the browser."""
from pathlib import Path

from interviewmaxxing_selection.credentials import load_api_key
from interviewmaxxing_selection.jev import JevClient

from .classification import (
    AIFormRouter,
    FieldRoute,
    FieldRouteDecision,
    FormRouteReport,
    RouteThresholds,
    SourceScope,
)
from .providers import AIHold, BoundedDecisions, CallBudget, NarrativeDraft, NarrativeWriter
from .routing import DynamicPacketResolver


def build_ai_runtime(*, env_file: Path, writer_model: str,
                     budget: CallBudget | None = None,
                     rag_connection_file: Path | None = None) -> tuple[AIFormRouter, DynamicPacketResolver]:
    key = load_api_key(env_file=env_file)
    shared = budget if budget is not None else CallBudget()
    decisions = BoundedDecisions(JevClient(key, timeout_seconds=15, max_attempts=1), shared)
    writer = NarrativeWriter(key, model=writer_model, budget=shared)
    router = AIFormRouter(decisions)
    retriever = None
    if rag_connection_file is not None:
        from .knowledge_runtime import build_knowledge_store

        retriever = build_knowledge_store(key, rag_connection_file, budget=shared)
    return router, DynamicPacketResolver(decisions, writer, router=router, retriever=retriever)


__all__ = [
    "AIFormRouter",
    "AIHold",
    "BoundedDecisions",
    "CallBudget",
    "DynamicPacketResolver",
    "FieldRoute",
    "FieldRouteDecision",
    "FormRouteReport",
    "NarrativeDraft",
    "NarrativeWriter",
    "RouteThresholds", "SourceScope",
    "build_ai_runtime",
]
