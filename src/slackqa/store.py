"""SQLite storage: raw messages, retrieval chunks, FTS index, ingest state.

Design notes
------------
* Slack ``ts`` ("1700000000.123456") is kept verbatim as TEXT because permalinks
  need it exactly, but every ordering and range query uses the REAL ``ts_num``
  column. Sorting timestamps as strings works only while the epoch stays ten
  digits, which is the kind of assumption that fails silently and late.
* Deletion is a real DELETE, not a flag. The whole point of honouring Slack
  deletions is that the content stops existing here too.
* ``chunks_fts`` is a plain FTS5 table keyed by ``rowid = chunks.id`` rather
  than an external-content table. It duplicates chunk text (a few MB at this
  scale) and in exchange the sync rules are "insert with the id, delete by the
  id" instead of FTS5's external-content delete protocol.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import numpy as np

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    ts         TEXT NOT NULL,
    ts_num     REAL NOT NULL,
    thread_ts  TEXT,
    user_id    TEXT NOT NULL,
    text       TEXT NOT NULL,
    UNIQUE(channel_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel_id, ts_num);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(channel_id, thread_ts);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,          -- 'thread' | 'window'
    anchor_ts    TEXT NOT NULL,          -- ts of first message; used for permalink
    start_ts     REAL NOT NULL,
    end_ts       REAL NOT NULL,
    participants TEXT NOT NULL,          -- JSON list of user ids
    msg_count    INTEGER NOT NULL,
    text         TEXT NOT NULL,
    embedding    BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_channel ON chunks(channel_id, start_ts);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text);

CREATE TABLE IF NOT EXISTS ingest_state (
    channel_id   TEXT PRIMARY KEY,
    last_ts      REAL NOT NULL DEFAULT 0,
    backfilled_at REAL
);

CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    cached_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS literature (
    identity    TEXT PRIMARY KEY,       -- DOI, arXiv id, or URL
    channel_id  TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    zotero_key  TEXT,
    has_pdf     INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,          -- added | needs-pdf | unresolved
    detail      TEXT NOT NULL DEFAULT '',
    source_ts   TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS query_expansions (
    question   TEXT PRIMARY KEY,
    terms      TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS query_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    question    TEXT NOT NULL,
    chunk_ids   TEXT NOT NULL,           -- JSON list
    refused     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
"""


@dataclass(frozen=True)
class Message:
    channel_id: str
    ts: str
    thread_ts: str | None
    user_id: str
    text: str

    @property
    def ts_num(self) -> float:
        return float(self.ts)


@dataclass(frozen=True)
class Chunk:
    channel_id: str
    kind: str
    anchor_ts: str
    start_ts: float
    end_ts: float
    participants: list[str]
    msg_count: int
    text: str
    id: int | None = None


def encode_embedding(vec: Sequence[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def decode_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


class Store:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    @classmethod
    async def open(cls, path: Path | str) -> Store:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(_SCHEMA)
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    # ---------------------------------------------------------------- messages

    async def upsert_messages(self, messages: Iterable[Message]) -> int:
        rows = [
            (m.channel_id, m.ts, m.ts_num, m.thread_ts, m.user_id, m.text)
            for m in messages
        ]
        if not rows:
            return 0
        await self._db.executemany(
            """INSERT INTO messages (channel_id, ts, ts_num, thread_ts, user_id, text)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(channel_id, ts) DO UPDATE SET
                   text = excluded.text,
                   thread_ts = excluded.thread_ts""",
            rows,
        )
        await self._db.commit()
        return len(rows)

    async def delete_message(self, channel_id: str, ts: str) -> None:
        await self._db.execute(
            "DELETE FROM messages WHERE channel_id = ? AND ts = ?", (channel_id, ts)
        )
        await self._db.commit()

    async def delete_messages(self, channel_id: str, tss: Sequence[str]) -> None:
        if not tss:
            return
        await self._db.executemany(
            "DELETE FROM messages WHERE channel_id = ? AND ts = ?",
            [(channel_id, ts) for ts in tss],
        )
        await self._db.commit()

    async def messages_in_range(
        self, channel_id: str, start_ts: float, end_ts: float
    ) -> list[Message]:
        """Messages with start_ts <= ts_num <= end_ts, chronological.

        Ordered by (ts_num, id) so identical timestamps stay deterministic.
        """
        async with self._db.execute(
            """SELECT channel_id, ts, thread_ts, user_id, text
               FROM messages
               WHERE channel_id = ? AND ts_num >= ? AND ts_num <= ?
               ORDER BY ts_num ASC, id ASC""",
            (channel_id, start_ts, end_ts),
        ) as cur:
            return [
                Message(r["channel_id"], r["ts"], r["thread_ts"], r["user_id"], r["text"])
                for r in await cur.fetchall()
            ]

    async def message_ts_since(self, channel_id: str, since_ts: float) -> set[str]:
        """Stored ts values newer than ``since_ts`` — used for the deletion diff."""
        async with self._db.execute(
            "SELECT ts FROM messages WHERE channel_id = ? AND ts_num >= ?",
            (channel_id, since_ts),
        ) as cur:
            return {r["ts"] for r in await cur.fetchall()}

    async def thread_reply_counts(self, channel_id: str) -> dict[str, int]:
        """How many replies we hold per thread root.

        Compared against Slack's own ``reply_count`` so an interrupted backfill
        skips only threads it genuinely finished. A set of "roots we have seen a
        reply for" is not good enough: a thread_broadcast reply shows up in
        channel history, which would mark a thread complete when its remaining
        replies were never fetched.
        """
        async with self._db.execute(
            """SELECT thread_ts, COUNT(*) AS n FROM messages
               WHERE channel_id = ? AND thread_ts IS NOT NULL AND thread_ts != ts
               GROUP BY thread_ts""",
            (channel_id,),
        ) as cur:
            return {r["thread_ts"]: r["n"] for r in await cur.fetchall() if r["thread_ts"]}

    async def distinct_users(self, channel_id: str) -> set[str]:
        async with self._db.execute(
            "SELECT DISTINCT user_id FROM messages WHERE channel_id = ?", (channel_id,)
        ) as cur:
            return {r["user_id"] for r in await cur.fetchall() if r["user_id"]}

    async def thread_ts_touching(
        self, channel_id: str, start_ts: float, end_ts: float
    ) -> list[str]:
        """Thread roots with any message inside the range (threads span windows)."""
        async with self._db.execute(
            """SELECT DISTINCT thread_ts FROM messages
               WHERE channel_id = ? AND thread_ts IS NOT NULL
                 AND ts_num >= ? AND ts_num <= ?""",
            (channel_id, start_ts, end_ts),
        ) as cur:
            return [r["thread_ts"] for r in await cur.fetchall() if r["thread_ts"]]

    async def messages_in_thread(self, channel_id: str, thread_ts: str) -> list[Message]:
        async with self._db.execute(
            """SELECT channel_id, ts, thread_ts, user_id, text
               FROM messages
               WHERE channel_id = ? AND (thread_ts = ? OR ts = ?)
               ORDER BY ts_num ASC, id ASC""",
            (channel_id, thread_ts, thread_ts),
        ) as cur:
            return [
                Message(r["channel_id"], r["ts"], r["thread_ts"], r["user_id"], r["text"])
                for r in await cur.fetchall()
            ]

    # ------------------------------------------------------------------ chunks

    async def delete_chunks_in_range(
        self, channel_id: str, start_ts: float, end_ts: float
    ) -> None:
        """Drop chunks overlapping the range, keeping the FTS table in step."""
        async with self._db.execute(
            """SELECT id FROM chunks
               WHERE channel_id = ? AND end_ts >= ? AND start_ts <= ?""",
            (channel_id, start_ts, end_ts),
        ) as cur:
            ids = [r["id"] for r in await cur.fetchall()]
        if not ids:
            return
        await self._db.executemany(
            "DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in ids]
        )
        await self._db.executemany("DELETE FROM chunks WHERE id = ?", [(i,) for i in ids])
        await self._db.commit()

    async def chunk_span_overlapping(
        self, channel_id: str, start_ts: float, end_ts: float
    ) -> tuple[float, float] | None:
        """Full time span of chunks overlapping the range, or None.

        Rebuilding a window requires re-reading every message the affected
        chunks were built from — a window chunk that straddles ``start_ts``
        would otherwise be regenerated from only its tail.
        """
        async with self._db.execute(
            """SELECT MIN(start_ts) AS lo, MAX(end_ts) AS hi FROM chunks
               WHERE channel_id = ? AND end_ts >= ? AND start_ts <= ?""",
            (channel_id, start_ts, end_ts),
        ) as cur:
            row = await cur.fetchone()
        if not row or row["lo"] is None:
            return None
        return float(row["lo"]), float(row["hi"])

    async def insert_chunks(
        self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]] | None = None
    ) -> list[int]:
        ids: list[int] = []
        for i, c in enumerate(chunks):
            emb = encode_embedding(embeddings[i]) if embeddings is not None else None
            cur = await self._db.execute(
                """INSERT INTO chunks
                   (channel_id, kind, anchor_ts, start_ts, end_ts,
                    participants, msg_count, text, embedding)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    c.channel_id,
                    c.kind,
                    c.anchor_ts,
                    c.start_ts,
                    c.end_ts,
                    json.dumps(c.participants),
                    c.msg_count,
                    c.text,
                    emb,
                ),
            )
            chunk_id = cur.lastrowid
            assert chunk_id is not None
            await self._db.execute(
                "INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", (chunk_id, c.text)
            )
            ids.append(chunk_id)
        await self._db.commit()
        return ids

    async def chunks_by_id(self, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        q = ",".join("?" for _ in ids)
        async with self._db.execute(
            f"""SELECT id, channel_id, kind, anchor_ts, start_ts, end_ts,
                       participants, msg_count, text
                FROM chunks WHERE id IN ({q})""",
            tuple(ids),
        ) as cur:
            out = {}
            for r in await cur.fetchall():
                d = dict(r)
                d["participants"] = json.loads(d["participants"])
                out[d["id"]] = d
            return out

    async def bm25_search(
        self, channel_id: str, query: str, limit: int
    ) -> list[tuple[int, float]]:
        """FTS5 match, restricted to one channel. Returns (chunk_id, bm25 score)."""
        async with self._db.execute(
            """SELECT f.rowid AS id, bm25(chunks_fts) AS score
               FROM chunks_fts f
               JOIN chunks c ON c.id = f.rowid
               WHERE chunks_fts MATCH ? AND c.channel_id = ?
               ORDER BY score ASC, f.rowid ASC
               LIMIT ?""",
            (query, channel_id, limit),
        ) as cur:
            return [(r["id"], r["score"]) for r in await cur.fetchall()]

    async def embeddings_for_channel(
        self, channel_id: str
    ) -> tuple[list[int], np.ndarray]:
        """All chunk vectors for a channel as (ids, matrix). Empty-safe."""
        async with self._db.execute(
            """SELECT id, embedding FROM chunks
               WHERE channel_id = ? AND embedding IS NOT NULL
               ORDER BY id ASC""",
            (channel_id,),
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return [], np.empty((0, 0), dtype=np.float32)
        ids = [r["id"] for r in rows]
        mat = np.vstack([decode_embedding(r["embedding"]) for r in rows])
        return ids, mat

    # ------------------------------------------------------------------- state

    async def get_last_ts(self, channel_id: str) -> float:
        async with self._db.execute(
            "SELECT last_ts FROM ingest_state WHERE channel_id = ?", (channel_id,)
        ) as cur:
            row = await cur.fetchone()
            return float(row["last_ts"]) if row else 0.0

    async def set_last_ts(self, channel_id: str, ts: float) -> None:
        await self._db.execute(
            """INSERT INTO ingest_state (channel_id, last_ts) VALUES (?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                   last_ts = MAX(last_ts, excluded.last_ts)""",
            (channel_id, ts),
        )
        await self._db.commit()

    async def mark_backfilled(self, channel_id: str, at: float) -> None:
        await self._db.execute(
            """INSERT INTO ingest_state (channel_id, last_ts, backfilled_at)
               VALUES (?, 0, ?)
               ON CONFLICT(channel_id) DO UPDATE SET backfilled_at = excluded.backfilled_at""",
            (channel_id, at),
        )
        await self._db.commit()

    async def is_backfilled(self, channel_id: str) -> bool:
        async with self._db.execute(
            "SELECT backfilled_at FROM ingest_state WHERE channel_id = ?", (channel_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row["backfilled_at"])

    # ------------------------------------------------------------------- users

    async def get_user_name(self, user_id: str) -> str | None:
        async with self._db.execute(
            "SELECT display_name FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row["display_name"] if row else None

    async def cache_user_name(self, user_id: str, display_name: str, at: float) -> None:
        await self._db.execute(
            """INSERT INTO users (user_id, display_name, cached_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   display_name = excluded.display_name, cached_at = excluded.cached_at""",
            (user_id, display_name, at),
        )
        await self._db.commit()

    # ------------------------------------------------------------- literature

    async def seen_reference(self, identity: str) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM literature WHERE identity = ?", (identity,)
        ) as cur:
            return await cur.fetchone() is not None

    async def record_reference(
        self,
        identity: str,
        channel_id: str,
        status: str,
        *,
        title: str = "",
        zotero_key: str | None = None,
        has_pdf: bool = False,
        detail: str = "",
        source_ts: str = "",
    ) -> None:
        import time as _time

        await self._db.execute(
            """INSERT INTO literature
               (identity, channel_id, title, zotero_key, has_pdf, status, detail,
                source_ts, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(identity) DO UPDATE SET
                   status = excluded.status, zotero_key = excluded.zotero_key,
                   has_pdf = excluded.has_pdf, detail = excluded.detail,
                   title = excluded.title""",
            (identity, channel_id, title[:400], zotero_key, int(has_pdf), status,
             detail[:400], source_ts, _time.time()),
        )
        await self._db.commit()

    async def literature_by_status(self, status: str) -> list[dict[str, Any]]:
        async with self._db.execute(
            "SELECT * FROM literature WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def literature_counts(self) -> dict[str, int]:
        async with self._db.execute(
            "SELECT status, COUNT(*) AS n FROM literature GROUP BY status"
        ) as cur:
            return {r["status"]: r["n"] for r in await cur.fetchall()}

    # -------------------------------------------------------- query expansion

    async def get_expansion(self, question: str) -> str | None:
        async with self._db.execute(
            "SELECT terms FROM query_expansions WHERE question = ?", (question.strip(),)
        ) as cur:
            row = await cur.fetchone()
            return row["terms"] if row else None

    async def put_expansion(self, question: str, terms: str) -> None:
        import time as _time

        await self._db.execute(
            """INSERT INTO query_expansions (question, terms, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(question) DO UPDATE SET
                   terms = excluded.terms, created_at = excluded.created_at""",
            (question.strip(), terms, _time.time()),
        )
        await self._db.commit()

    # --------------------------------------------------------------- query log

    async def log_query(
        self,
        channel_id: str,
        user_id: str,
        question: str,
        chunk_ids: Sequence[int],
        refused: bool,
        at: float,
    ) -> None:
        await self._db.execute(
            """INSERT INTO query_log
               (channel_id, user_id, question, chunk_ids, refused, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (channel_id, user_id, question, json.dumps(list(chunk_ids)), int(refused), at),
        )
        await self._db.commit()
