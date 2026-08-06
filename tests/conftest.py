from __future__ import annotations

import numpy as np
import pytest

from slackqa.embeddings import l2_normalize
from slackqa.store import Store


class FakeEmbedder:
    """Deterministic bag-of-words vectors.

    Keeps the suite offline and fast — the real model is a ~130MB download and
    its behaviour is not what these tests are checking.
    """

    VOCAB = [
        "postgres",
        "mysql",
        "deploy",
        "onboarding",
        "budget",
        "runbook",
        "alpha",
        "bravo",
        "charlie",
    ]

    def _vec(self, text: str) -> np.ndarray:
        low = text.lower()
        v = np.array([float(low.count(w)) for w in self.VOCAB], dtype=np.float32)
        if not v.any():
            v = np.ones(len(self.VOCAB), dtype=np.float32)
        return l2_normalize(v)

    def embed_documents(self, texts):
        if not len(texts):
            return np.empty((0, len(self.VOCAB)), dtype=np.float32)
        return np.vstack([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()
