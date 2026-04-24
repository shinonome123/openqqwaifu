from __future__ import annotations

from .embedding_clients import (
    EmbeddingClient,
    EmbeddingClientError,
    UnifiedEmbeddingClient,
    build_embedding_client,
)

EmbeddingError = EmbeddingClientError
EmbeddingClient = UnifiedEmbeddingClient

__all__ = [
    "EmbeddingClient",
    "EmbeddingError",
    "EmbeddingClientError",
    "build_embedding_client",
]
