"""Private retrieval projection of canonical verified candidate evidence."""

from .embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    Embedder,
    EmbeddingError,
    EmbeddingResult,
    KnowledgeError,
    OpenRouterEmbedder,
)
from .store import (
    KnowledgeRetriever,
    PgKnowledgeStore,
    RetrievalResult,
    fact_fingerprint,
    job_fingerprint,
    pg_connection_factory,
)

__all__ = [
    "EMBEDDING_DIMENSIONS", "EMBEDDING_MODEL", "Embedder", "EmbeddingError",
    "EmbeddingResult", "KnowledgeError", "KnowledgeRetriever", "OpenRouterEmbedder",
    "PgKnowledgeStore", "RetrievalResult", "fact_fingerprint", "job_fingerprint",
    "pg_connection_factory",
]
