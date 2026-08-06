"""Hybrid retrieval: BM25 + dense vectors, fused with Reciprocal Rank Fusion.

Both retrievers earn their place. BM25 carries the exact tokens Slack is full of
— ticket IDs, error strings, service names, usernames — which embedding models
blur. Vectors carry paraphrase, matching "how do we deploy" against a thread
that only ever says "shipping to prod".

Scope is strictly one channel per query. There is no cross-channel code path, so
there is no ACL to get wrong.

On refusal: RRF scores measure rank agreement, not similarity, so they make a
poor absolute relevance gate and any threshold here would be a number tuned
against no data. The refusal decision is therefore split — this layer refuses
only when retrieval is genuinely empty, and the answerer's prompt makes the
model refuse when the retrieved chunks don't actually contain the answer.
``min_cosine`` is a coarse junk filter, not a calibrated relevance cutoff.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from slackqa.embeddings import Embedder
from slackqa.store import Store

_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")

# Dropped from BM25 queries. BM25 discounts common terms via IDF, but over a
# few thousand chunks that signal is weak, and a natural-language question is
# mostly function words — leaving them in makes BM25 rank by stopword density
# instead of by the rare, exact tokens (ticket IDs, service names, error
# strings) that are the entire reason the keyword path exists.
_STOPWORDS = frozenset(
    """
    a about all also am an and any are as at be because been but by can cant
    could did do does doing dont for from get got had has have he her here hers
    him his how i if in into is it its just me more most my no nor not of off on
    once only or other our out over own re same she should so some such than
    that the their them then there these they this those through to too under
    until up us very was we were what when where which while who whom why will
    with would you your yours
    """.split()
)


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Raw user text is not a valid FTS5 query — quotes, ``*``, ``:``, ``^`` and
    bare ``AND``/``OR``/``NOT``/``NEAR`` are all syntax, so passing a question
    through unescaped raises OperationalError on punctuation alone. Each
    surviving token is double-quoted (internal quotes doubled) and OR-ed: OR
    rather than AND, because requiring every term of a natural-language question
    would match almost nothing.

    Returns "" when nothing contentful survives, which the retriever treats as
    "BM25 has no opinion" and lets the vector path decide alone.
    """
    tokens = [
        t for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS and len(t) > 1
    ]
    if not tokens:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def rrf_fuse(rankings: Sequence[Sequence[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion: score(d) = sum over lists of 1/(k + rank(d)).

    Rank-based rather than score-based, so BM25 scores and cosine similarities
    never need to be made commensurable.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    score: float
    chunk: dict[str, Any]

    @property
    def text(self) -> str:
        return self.chunk["text"]

    @property
    def anchor_ts(self) -> str:
        return self.chunk["anchor_ts"]


class Retriever:
    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        *,
        candidates: int = 30,
        rrf_k: int = 60,
        min_cosine: float = 0.0,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._candidates = candidates
        self._rrf_k = rrf_k
        self._min_cosine = min_cosine

    async def _bm25_ranking(self, channel_id: str, query: str) -> list[int]:
        expr = fts_query(query)
        if not expr:
            return []
        hits = await self._store.bm25_search(channel_id, expr, self._candidates)
        # bm25() returns lower-is-better; store already orders ascending.
        return [chunk_id for chunk_id, _ in hits]

    async def _vector_ranking(self, channel_id: str, query: str) -> list[int]:
        ids, mat = await self._store.embeddings_for_channel(channel_id)
        if not ids or mat.size == 0:
            return []
        qvec = self._embedder.embed_query(query)
        if qvec.shape[-1] != mat.shape[1]:
            # Embedding model changed since these chunks were indexed; a
            # dimension mismatch means the stored vectors are stale, not that
            # the query is bad, so degrade to BM25 instead of raising.
            return []
        sims = mat @ qvec
        order = np.argsort(-sims)[: self._candidates]
        return [ids[i] for i in order if sims[i] >= self._min_cosine]

    async def retrieve(self, channel_id: str, query: str, top_k: int) -> list[Hit]:
        bm25 = await self._bm25_ranking(channel_id, query)
        vector = await self._vector_ranking(channel_id, query)

        fused = rrf_fuse([bm25, vector], k=self._rrf_k)
        if not fused:
            return []

        # Sort by score, tie-break on id so results are reproducible.
        ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
        chunk_ids = [cid for cid, _ in ranked]
        chunks = await self._store.chunks_by_id(chunk_ids)

        return [
            Hit(chunk_id=cid, score=score, chunk=chunks[cid])
            for cid, score in ranked
            if cid in chunks
        ]
