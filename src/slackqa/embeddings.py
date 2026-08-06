"""Local embeddings via fastembed (ONNX).

Runs on CPU with no torch dependency. The reason to keep this local rather than
call an embedding API is asymmetry: generation ships only the handful of
retrieved chunks to the provider, but embedding would ship *every message ever
indexed*. Local embedding keeps the archive on the host and makes re-indexing
an offline operation.

The model is downloaded once on first use (~130MB) and cached under
``~/.cache/fastembed``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import numpy as np

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class Embedder(Protocol):
    """Retrieval depends on this, not on fastembed, so tests can substitute."""

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


def l2_normalize(mat: np.ndarray) -> np.ndarray:
    if mat.size == 0:
        return mat
    norms = np.linalg.norm(mat, axis=-1, keepdims=True)
    # Guard against zero vectors rather than emitting nan.
    norms = np.where(norms == 0, 1.0, norms)
    return mat / norms


class FastEmbedEmbedder:
    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model = None  # loaded lazily; import cost is non-trivial

    def _get(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self._model_name)
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        vecs = list(self._get().embed(list(texts)))
        return l2_normalize(np.vstack(vecs).astype(np.float32))

    def embed_query(self, text: str) -> np.ndarray:
        # bge models expect an instruction prefix on queries; query_embed applies it.
        vec = next(iter(self._get().query_embed(text)))
        return l2_normalize(np.asarray(vec, dtype=np.float32))
