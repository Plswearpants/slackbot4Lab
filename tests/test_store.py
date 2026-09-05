from __future__ import annotations

import numpy as np
import pytest

from slackqa.store import Chunk, Message, Store, decode_embedding, encode_embedding

CH = "C0TEST"


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "test.db")
    yield s
    await s.close()


def msg(ts: str, text: str, user: str = "U1", thread_ts: str | None = None) -> Message:
    return Message(channel_id=CH, ts=ts, thread_ts=thread_ts, user_id=user, text=text)


async def test_upsert_and_range(store):
    await store.upsert_messages([msg("100.000001", "a"), msg("200.000001", "b")])
    rows = await store.messages_in_range(CH, 0, 1e12)
    assert [m.text for m in rows] == ["a", "b"]


async def test_ordering_is_chronological_and_stable(store):
    # Same ts_num for every message: ordering must still be deterministic and
    # chronological by insertion, not reversed.
    await store.upsert_messages([msg(f"100.00000{i}", f"m{i}") for i in range(5)])
    rows = await store.messages_in_range(CH, 0, 1e12)
    assert [m.text for m in rows] == ["m0", "m1", "m2", "m3", "m4"]


async def test_upsert_is_idempotent_and_updates_text(store):
    await store.upsert_messages([msg("100.000001", "original")])
    await store.upsert_messages([msg("100.000001", "edited")])
    rows = await store.messages_in_range(CH, 0, 1e12)
    assert len(rows) == 1
    assert rows[0].text == "edited"


async def test_delete_message_actually_removes(store):
    await store.upsert_messages([msg("100.000001", "secret")])
    await store.delete_message(CH, "100.000001")
    assert await store.messages_in_range(CH, 0, 1e12) == []


async def test_message_ts_since_for_deletion_diff(store):
    await store.upsert_messages(
        [msg("100.000001", "old"), msg("500.000001", "new")]
    )
    assert await store.message_ts_since(CH, 400) == {"500.000001"}


async def test_thread_messages_include_root(store):
    await store.upsert_messages(
        [
            msg("100.000001", "root"),
            msg("110.000001", "reply1", thread_ts="100.000001"),
            msg("120.000001", "reply2", thread_ts="100.000001"),
            msg("130.000001", "unrelated"),
        ]
    )
    thread = await store.messages_in_thread(CH, "100.000001")
    assert [m.text for m in thread] == ["root", "reply1", "reply2"]


def chunk(text: str, start: float = 100.0, end: float = 200.0) -> Chunk:
    return Chunk(
        channel_id=CH,
        kind="window",
        anchor_ts=f"{start:.6f}",
        start_ts=start,
        end_ts=end,
        participants=["U1"],
        msg_count=2,
        text=text,
    )


async def test_bm25_search_finds_chunk(store):
    await store.insert_chunks([chunk("we decided to migrate to postgres")])
    hits = await store.bm25_search(CH, "postgres", limit=5)
    assert len(hits) == 1


async def test_bm25_is_channel_scoped(store):
    other = Chunk(
        channel_id="C0OTHER",
        kind="window",
        anchor_ts="100.000000",
        start_ts=100.0,
        end_ts=200.0,
        participants=["U9"],
        msg_count=1,
        text="postgres migration in another channel",
    )
    await store.insert_chunks([chunk("postgres here"), other])
    hits = await store.bm25_search(CH, "postgres", limit=10)
    ids = [h[0] for h in hits]
    found = await store.chunks_by_id(ids)
    assert all(c["channel_id"] == CH for c in found.values())
    assert len(hits) == 1


async def test_deleting_chunks_clears_fts(store):
    await store.insert_chunks([chunk("ephemeral content")])
    assert await store.bm25_search(CH, "ephemeral", limit=5)
    await store.delete_chunks_in_range(CH, 0, 1e12)
    assert await store.bm25_search(CH, "ephemeral", limit=5) == []


async def test_embedding_roundtrip(store):
    vec = [0.1, 0.2, 0.3, 0.4]
    await store.insert_chunks([chunk("with vector")], embeddings=[vec])
    ids, mat = await store.embeddings_for_channel(CH)
    assert len(ids) == 1
    assert mat.shape == (1, 4)
    np.testing.assert_allclose(mat[0], vec, rtol=1e-6)


def test_encode_decode_embedding():
    vec = [1.5, -2.5, 3.0]
    np.testing.assert_allclose(decode_embedding(encode_embedding(vec)), vec)


async def test_last_ts_never_moves_backwards(store):
    await store.set_last_ts(CH, 500.0)
    await store.set_last_ts(CH, 100.0)
    assert await store.get_last_ts(CH) == 500.0


async def test_query_log(store):
    await store.log_query(CH, "U1", "what did we decide?", [1, 2], False, 123.0)
    async with store._db.execute("SELECT * FROM query_log") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["question"] == "what did we decide?"


async def test_added_columns_are_migrated_into_an_existing_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS leaves an older table untouched, so a new
    column has to be added explicitly — otherwise every query naming it fails
    on exactly the databases that have real data in them."""
    import aiosqlite

    path = tmp_path / "old.db"
    # A database created before the column existed.
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """CREATE TABLE literature (
                   identity TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
                   title TEXT NOT NULL DEFAULT '', zotero_key TEXT,
                   has_pdf INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                   detail TEXT NOT NULL DEFAULT '',
                   source_ts TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)"""
        )
        await db.commit()

    s = await Store.open(path)
    await s.record_reference("10.1/x", CH, "indexed", title="T", abstract="A")
    assert await s.resolved_papers(CH) == {"10.1/x": "T — A"}
    await s.close()
