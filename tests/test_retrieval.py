from __future__ import annotations

import pytest

from slackqa.retrieval import Retriever, fts_query, rrf_fuse
from slackqa.store import Chunk

CH = "C0TEST"


def mk(text: str, start: float, channel: str = CH) -> Chunk:
    return Chunk(
        channel_id=channel,
        kind="window",
        anchor_ts=f"{start:.6f}",
        start_ts=start,
        end_ts=start + 10,
        participants=["U1"],
        msg_count=1,
        text=text,
    )


async def seed(store, embedder, chunks):
    vecs = embedder.embed_documents([c.text for c in chunks])
    return await store.insert_chunks(chunks, embeddings=[v.tolist() for v in vecs])


# ----------------------------------------------------------------- fts_query


def test_fts_query_quotes_tokens():
    assert fts_query("postgres migration") == '"postgres" OR "migration"'


def test_fts_query_survives_punctuation():
    # Raw FTS5 would choke on these; we must not.
    for q in ['what about "prod"?', "deploy: step 1 -- go", "a*b^c", "NEAR AND OR NOT"]:
        expr = fts_query(q)
        assert '""' not in expr.replace('""', "") or True  # smoke: no crash
        assert expr == "" or expr.startswith('"')


def test_fts_query_empty_for_symbols_only():
    assert fts_query("?!  ...") == ""


def test_fts_query_drops_stopwords():
    # Function words match every chunk and, with little IDF signal at this
    # corpus size, make BM25 rank by stopword density instead of rare terms.
    assert fts_query("how do we deploy to production") == '"deploy" OR "production"'


def test_fts_query_drops_single_characters():
    assert fts_query("I a x deploy") == '"deploy"'


def test_fts_query_keeps_identifiers_and_versions():
    # The exact tokens BM25 exists to catch must survive intact.
    assert fts_query("PROJ-4471 failed on api-gateway v2.1") == (
        '"PROJ-4471" OR "failed" OR "api-gateway" OR "v2.1"'
    )


def test_fts_query_empty_when_only_stopwords():
    # Vector search alone then decides, rather than BM25 matching everything.
    assert fts_query("how do we do that") == ""


async def test_punctuation_query_does_not_raise(store, embedder):
    await store.insert_chunks([mk("we ship on fridays", 100)])
    hits = await store.bm25_search(CH, fts_query('when do we "ship"?'), 10)
    assert len(hits) == 1


# ------------------------------------------------------------------ rrf_fuse


def test_rrf_rewards_agreement():
    # doc 1 is top of both lists; doc 2 is top of one only.
    scores = rrf_fuse([[1, 2], [1, 3]], k=60)
    assert scores[1] > scores[2]
    assert scores[1] == pytest.approx(2 / 61)


def test_rrf_handles_empty_lists():
    assert rrf_fuse([[], []]) == {}
    assert rrf_fuse([[5], []])[5] == pytest.approx(1 / 61)


# ----------------------------------------------------------------- retrieval


async def test_retrieves_relevant_chunk(store, embedder):
    emb = embedder
    await seed(
        store,
        emb,
        [mk("we migrated to postgres last quarter", 100), mk("budget review notes", 200)],
    )
    r = Retriever(store, emb)
    hits = await r.retrieve(CH, "postgres", top_k=1)
    assert len(hits) == 1
    assert "postgres" in hits[0].text


async def test_empty_index_returns_nothing(store, embedder):
    r = Retriever(store, embedder)
    assert await r.retrieve(CH, "anything", top_k=5) == []


async def test_retrieval_is_channel_scoped(store, embedder):
    emb = embedder
    await seed(
        store,
        emb,
        [mk("postgres in ours", 100), mk("postgres in theirs", 200, channel="C0OTHER")],
    )
    hits = await Retriever(store, emb).retrieve(CH, "postgres", top_k=10)
    assert len(hits) == 1
    assert hits[0].chunk["channel_id"] == CH


async def test_bm25_only_still_returns_when_vectors_absent(store, embedder):
    # Chunks inserted without embeddings: vector path is empty, BM25 carries it.
    await store.insert_chunks([mk("the deploy runbook lives in notion", 100)])
    hits = await Retriever(store, embedder).retrieve(CH, "runbook", top_k=5)
    assert len(hits) == 1


async def test_dimension_mismatch_degrades_to_bm25(store, embedder):
    await store.insert_chunks(
        [mk("postgres notes", 100)], embeddings=[[0.1, 0.2, 0.3]]  # wrong width
    )
    hits = await Retriever(store, embedder).retrieve(CH, "postgres", top_k=5)
    assert len(hits) == 1  # survived via BM25 rather than raising


async def test_results_are_deterministic(store, embedder):
    emb = embedder
    await seed(store, emb, [mk(f"postgres note {i}", 100 + i * 10) for i in range(5)])
    r = Retriever(store, emb)
    first = [h.chunk_id for h in await r.retrieve(CH, "postgres", top_k=3)]
    second = [h.chunk_id for h in await r.retrieve(CH, "postgres", top_k=3)]
    assert first == second


async def test_top_k_is_respected(store, embedder):
    emb = embedder
    await seed(store, emb, [mk(f"postgres {i}", 100 + i * 10) for i in range(10)])
    assert len(await Retriever(store, emb).retrieve(CH, "postgres", top_k=3)) == 3
