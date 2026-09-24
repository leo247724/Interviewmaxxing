"""Private database configuration for retrieval; construction does not contact providers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from interviewmaxxing_selection.credentials import ApiKey

from .providers import CallBudget, CallReceipt

if TYPE_CHECKING:
    import psycopg

    from interviewmaxxing_generation.knowledge import PgKnowledgeStore

def connection_settings(path: Path) -> dict[str, Any]:
    """Read the explicit server-side JSON connection file without logging credentials."""
    try:
        if not path.is_absolute() or path.stat().st_size > 16_384:
            raise ValueError
        settings = json.loads(path.read_text(encoding="utf-8"))
        allowed = {"host", "port", "dbname", "user", "password", "sslmode", "connect_timeout"}
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise ValueError
        if not all(isinstance(settings.get(k), str) and settings[k] for k in
                   ("host", "dbname", "user", "password")):
            raise ValueError
        if settings.get("sslmode", "require") not in {"require", "verify-ca", "verify-full"}:
            raise ValueError
        return {**settings, "sslmode": settings.get("sslmode", "require"),
                "connect_timeout": 10}
    except (OSError, ValueError, TypeError):
        raise ValueError("RAG connection file is unavailable or invalid") from None


def build_knowledge_store(key: ApiKey, connection_file: Path, *,
                          budget: CallBudget | None = None) -> PgKnowledgeStore:
    from interviewmaxxing_generation.knowledge import OpenRouterEmbedder, PgKnowledgeStore

    settings = connection_settings(connection_file)

    def connect() -> psycopg.Connection[Any]:
        import psycopg

        try:
            return psycopg.connect(**settings)
        except psycopg.Error:
            raise RuntimeError("RAG database connection failed") from None

    def before_request(body: bytes) -> object:
        assert budget is not None
        # UTF-8 bytes bound input tokens conservatively for the fixed embedding model.
        reserve = (len(body) + 2048) * 0.02 / 1_000_000
        budget.reserve(body, reserve)
        return reserve

    def after_request(token: object, metadata: dict[str, Any]) -> None:
        assert budget is not None and isinstance(token, float)
        budget.record(CallReceipt(purpose="knowledge_embedding", model=metadata["model"],
            resolved_model=metadata.get("resolved_model"),
            latency_seconds=metadata["latency_seconds"], cost_usd=metadata.get("cost_usd"),
            reserved_usd=token, status=metadata["status"]))

    embedder = (OpenRouterEmbedder(key.reveal(), before_request=before_request,
                                  after_request=after_request)
                if budget is not None else OpenRouterEmbedder(key.reveal()))
    return PgKnowledgeStore(connect, embedder)
