"""Rebuilding chunks must never orphan messages it did not mean to touch.

Threads become one chunk however long they run; everything else is grouped by
time. reindex_window used to pull a whole thread in, widen its time range to
the thread's span, and then delete every chunk overlapping that range — while
rebuilding only from the messages it had loaded before widening. Everything
said in between, in other conversations, lost its chunk and became
unsearchable.

Found after the full reconcile added old #createc replies: 14 messages newly
outside any chunk. And live: since message.channels was subscribed, one reply
today to a thread from 2023 would have done the same to most of a channel.
"""

from __future__ import annotations

from collections import Counter

from slackqa import ingest
from slackqa.store import Message

CH = "C0TEST"
DAY = 86400.0
R = 1_700_000_000.0  # an old thread root


def m(t, text, thread=None, user="U1"):
    return Message(channel_id=CH, ts=f"{t:.6f}",
                   thread_ts=f"{thread:.6f}" if thread is not None else None,
                   user_id=user, text=text)


def ev(t, text, thread=None, user="U1"):
    e = {"ts": f"{t:.6f}", "user": user, "text": text}
    if thread is not None:
        e["thread_ts"] = f"{thread:.6f}"
    return e


async def build(store, embedder, messages):
    await store.upsert_messages(messages)
    await ingest.reindex_channel(store, embedder, CH)


async def chunks(store):
    ids, _ = await store.embeddings_for_channel(CH)
    return list((await store.chunks_by_id(ids)).values())


async def orphans(store):
    """Messages whose text appears in no chunk — stored but unsearchable."""
    msgs = await store.messages_in_range(CH, 0, float("inf"))
    texts = [c["text"] for c in await chunks(store)]
    return [x.text for x in msgs if not any(x.text in t for t in texts)]


async def duplicate_anchors(store):
    counts = Counter((c["kind"], c["anchor_ts"]) for c in await chunks(store))
    return [k for k, n in counts.items() if n > 1]


async def old_thread_with_conversations_after(store, embedder):
    """A thread from long ago, then ten separate conversations after it."""
    rows = [m(R, "thread root about the cryostat", thread=R),
            m(R + 60, "first reply about the cryostat", thread=R)]
    rows += [m(R + i * DAY, f"unrelated conversation {i}") for i in range(1, 11)]
    await build(store, embedder, rows)
    assert await orphans(store) == []


# ------------------------------------------------------------------- live


async def test_a_late_reply_to_an_old_thread_orphans_nothing(store, embedder):
    """The live catastrophe: one reply, months later, to an old thread."""
    await old_thread_with_conversations_after(store, embedder)

    await ingest.handle_new_message(
        store, embedder, CH, ev(R + 30 * DAY, "late reply: cryostat fixed", thread=R))

    assert await orphans(store) == []
    assert await duplicate_anchors(store) == []
    thread = [c for c in await chunks(store) if c["kind"] == "thread"]
    assert len(thread) == 1 and "late reply" in thread[0]["text"]


async def test_editing_a_late_reply_orphans_nothing(store, embedder):
    await old_thread_with_conversations_after(store, embedder)
    late = ev(R + 30 * DAY, "late reply v1", thread=R)
    await ingest.handle_new_message(store, embedder, CH, late)

    await ingest.handle_edit(store, embedder, CH,
                             {"message": {**late, "text": "late reply v2"}})

    assert await orphans(store) == []
    body = " ".join(c["text"] for c in await chunks(store))
    assert "late reply v2" in body and "late reply v1" not in body


async def test_deleting_a_reply_removes_its_text_from_the_thread(store, embedder):
    """P3: the deleted reply is the only message near its time, so the
    thread has to be found through its chunk, not through a message."""
    rows = [m(R, "root", thread=R), m(R + 60, "keep this", thread=R),
            m(R + 20 * DAY, "password is hunter2", thread=R)]
    await build(store, embedder, rows)

    await ingest.handle_delete(store, embedder, CH, {"deleted_ts": f"{R + 20 * DAY:.6f}"})

    body = " ".join(c["text"] for c in await chunks(store))
    assert "hunter2" not in body and "keep this" in body
    assert await orphans(store) == []


async def test_a_top_level_change_leaves_a_spanning_thread_intact(store, embedder):
    """The reverse direction: a window change inside a long thread's span must
    not drop the thread's chunk either."""
    await old_thread_with_conversations_after(store, embedder)
    await ingest.handle_new_message(
        store, embedder, CH, ev(R + 30 * DAY, "late reply", thread=R))

    await ingest.handle_new_message(store, embedder, CH, ev(R + 5 * DAY + 30, "aside"))

    assert await orphans(store) == []
    assert await duplicate_anchors(store) == []


# -------------------------------------------------------------- reconcile


async def test_recovering_an_old_reply_orphans_nothing(store, embedder):
    """What the reconcile did to #createc: a reply added to an old thread whose
    span covered other conversations. One reply, so no later rebuild can
    accidentally repair the damage — with several, the second rebuild widened
    to the now-long thread chunk, reloaded everything, and hid the bug."""
    await old_thread_with_conversations_after(store, embedder)
    added = [m(R + 6 * DAY + 3600, "recovered reply", thread=R)]
    await store.upsert_messages(added)

    await ingest.reindex_affected(store, embedder, CH, [x.ts_num for x in added])

    assert await orphans(store) == []
    assert await duplicate_anchors(store) == []


async def test_incremental_rebuild_matches_a_full_rebuild(store, embedder):
    """The strongest check: after any sequence of live changes, the chunks
    should be exactly what a from-scratch rebuild would produce."""
    await old_thread_with_conversations_after(store, embedder)
    # The thread-widening change comes last, so nothing afterwards can mask it.
    await ingest.handle_new_message(store, embedder, CH, ev(R + 3 * DAY + 10, "aside"))
    await ingest.handle_delete(store, embedder, CH, {"deleted_ts": f"{R + 7 * DAY:.6f}"})
    await ingest.handle_new_message(store, embedder, CH,
                                    ev(R + 30 * DAY, "late reply", thread=R))

    incremental = sorted((c["kind"], c["anchor_ts"], c["text"]) for c in await chunks(store))
    await ingest.reindex_channel(store, embedder, CH)
    full = sorted((c["kind"], c["anchor_ts"], c["text"]) for c in await chunks(store))
    assert incremental == full
