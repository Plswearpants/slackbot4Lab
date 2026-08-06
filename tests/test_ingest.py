from __future__ import annotations

import numpy as np
import pytest

from slackqa import ingest

CH = "C0TEST"
BOT = "U0BOT"


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    """Strip pacing pauses and retry backoff so the suite stays fast."""
    monkeypatch.setattr(ingest, "PAGE_PAUSE_SECONDS", 0)

    async def instant(_seconds):
        return None

    monkeypatch.setattr(ingest.asyncio, "sleep", instant)


class FakeResponse(dict):
    """Mimics slack_sdk's response object closely enough for these paths."""


class FakeClient:
    """Minimal conversations.history / .replies with cursor pagination."""

    def __init__(self, history, replies=None, page_size=100):
        self._history = list(history)
        self._replies = replies or {}
        self._page_size = page_size
        self.history_calls = 0
        self.history_kwargs: list[dict] = []

    async def conversations_history(self, channel, limit, oldest=None, cursor=None):
        self.history_calls += 1
        self.history_kwargs.append(
            {"limit": limit, "oldest": oldest, "cursor": cursor}
        )
        oldest = "0" if oldest is None else oldest
        msgs = [m for m in self._history if float(m["ts"]) > float(oldest)]
        msgs.sort(key=lambda m: float(m["ts"]))
        start = int(cursor) if cursor else 0
        page = msgs[start : start + self._page_size]
        nxt = start + self._page_size
        meta = {"next_cursor": str(nxt)} if nxt < len(msgs) else {}
        return FakeResponse(messages=page, response_metadata=meta)

    async def conversations_replies(self, channel, ts, limit, cursor=None):
        return FakeResponse(messages=self._replies.get(ts, []), response_metadata={})


def human(ts: float, text: str, user="U1", **extra):
    return {"ts": f"{ts:.6f}", "user": user, "text": text, **extra}


# -------------------------------------------------------------------- backfill


async def test_backfill_indexes_human_messages(store, embedder):
    client = FakeClient([human(100, "we chose postgres"), human(200, "agreed")])
    n = await ingest.backfill(store, client, embedder, CH, bot_user_id=BOT)
    assert n == 2
    hits = await store.bm25_search(CH, '"postgres"', 10)
    assert len(hits) == 1  # both messages landed in one window chunk


async def test_backfill_excludes_bots_and_system(store, embedder):
    client = FakeClient(
        [
            human(100, "real message"),
            {"ts": "150.000000", "bot_id": "B1", "text": "build passed"},
            {"ts": "160.000000", "user": "U2", "subtype": "channel_join", "text": "joined"},
            human(170, f"<@{BOT}> what is the plan?"),
        ]
    )
    assert await ingest.backfill(store, client, embedder, CH, bot_user_id=BOT) == 1


async def test_backfill_paginates(store, embedder):
    msgs = [human(100 + i, f"message {i}") for i in range(250)]
    client = FakeClient(msgs, page_size=100)
    assert await ingest.backfill(store, client, embedder, CH) == 250
    assert client.history_calls == 3


async def test_backfill_pulls_thread_replies(store, embedder):
    root = human(100, "root question", reply_count=2, thread_ts="100.000000")
    client = FakeClient(
        [root],
        replies={
            "100.000000": [
                root,
                human(110, "first reply", thread_ts="100.000000"),
                human(120, "second reply", thread_ts="100.000000"),
            ]
        },
    )
    assert await ingest.backfill(store, client, embedder, CH) == 3
    msgs = await store.messages_in_range(CH, 0, 1e12)
    assert len(msgs) == 3


async def test_backfill_marks_state(store, embedder):
    client = FakeClient([human(100, "x")])
    await ingest.backfill(store, client, embedder, CH)
    assert await store.is_backfilled(CH)
    assert await store.get_last_ts(CH) == pytest.approx(100.0)


# -------------------------------------------------------------------- catch_up


async def test_catch_up_only_fetches_new(store, embedder):
    client = FakeClient([human(100, "old"), human(200, "new")])
    await ingest.backfill(store, client, embedder, CH)
    client._history.append(human(300, "newer"))
    assert await ingest.catch_up(store, client, embedder, CH) == 1


async def test_watermark_advances_past_filtered_traffic(store, embedder):
    client = FakeClient([human(100, "real")])
    await ingest.backfill(store, client, embedder, CH)
    client._history.append({"ts": "500.000000", "bot_id": "B1", "text": "noise"})
    await ingest.catch_up(store, client, embedder, CH)
    assert await store.get_last_ts(CH) == pytest.approx(500.0)


# --------------------------------------------------------------- live handlers


async def test_new_message_is_indexed_and_searchable(store, embedder):
    await ingest.handle_new_message(
        store, embedder, CH, human(100, "the runbook is in notion"), bot_user_id=BOT
    )
    assert await store.bm25_search(CH, '"runbook"', 10)


async def test_bot_message_is_ignored(store, embedder):
    ok = await ingest.handle_new_message(
        store, embedder, CH, {"ts": "100.0", "bot_id": "B1", "text": "x"}, bot_user_id=BOT
    )
    assert ok is False


async def test_edit_updates_index(store, embedder):
    await ingest.handle_new_message(store, embedder, CH, human(100, "we use mysql"))
    await ingest.handle_edit(
        store, embedder, CH, {"message": human(100, "we use postgres")}
    )
    assert await store.bm25_search(CH, '"postgres"', 10)
    assert not await store.bm25_search(CH, '"mysql"', 10)


async def test_delete_removes_from_index(store, embedder):
    await ingest.handle_new_message(store, embedder, CH, human(100, "sk-secret-token"))
    assert await store.bm25_search(CH, '"secret"', 10)
    await ingest.handle_delete(store, embedder, CH, {"deleted_ts": "100.000000"})
    assert await store.bm25_search(CH, '"secret"', 10) == []
    assert await store.messages_in_range(CH, 0, 1e12) == []


async def test_delete_via_previous_message_shape(store, embedder):
    await ingest.handle_new_message(store, embedder, CH, human(100, "gone soon"))
    await ingest.handle_delete(
        store, embedder, CH, {"previous_message": {"ts": "100.000000"}}
    )
    assert await store.messages_in_range(CH, 0, 1e12) == []


async def test_delete_rebuilds_neighbouring_window(store, embedder):
    # Three messages in one window; deleting the middle must leave a chunk
    # containing the other two, not a stale chunk still quoting the deleted one.
    for ts, text in [(100, "alpha"), (150, "bravo"), (200, "charlie")]:
        await ingest.handle_new_message(store, embedder, CH, human(ts, text))
    await ingest.handle_delete(store, embedder, CH, {"deleted_ts": "150.000000"})

    assert await store.bm25_search(CH, '"bravo"', 10) == []
    alpha = await store.bm25_search(CH, '"alpha"', 10)
    charlie = await store.bm25_search(CH, '"charlie"', 10)
    assert alpha and charlie
    assert alpha[0][0] == charlie[0][0]  # same surviving chunk


# ---------------------------------------------------------------- reconcile


async def test_reconcile_purges_offline_deletions(store, embedder):
    import time as _time

    now = _time.time()
    client = FakeClient([human(now - 100, "kept"), human(now - 50, "leaked-credential")])
    await ingest.backfill(store, client, embedder, CH)
    assert await store.bm25_search(CH, '"leaked"', 10)

    # Simulate a deletion that happened while we were not listening: Slack
    # simply stops returning it, and no event is ever replayed.
    client._history = [m for m in client._history if "leaked" not in m["text"]]

    purged = await ingest.reconcile_deletions(store, client, embedder, CH)
    assert purged == 1
    assert await store.bm25_search(CH, '"leaked"', 10) == []
    assert await store.bm25_search(CH, '"kept"', 10)


async def test_reconcile_noop_when_in_sync(store, embedder):
    import time as _time

    client = FakeClient([human(_time.time() - 10, "stable")])
    await ingest.backfill(store, client, embedder, CH)
    assert await ingest.reconcile_deletions(store, client, embedder, CH) == 0


# ----------------------------------------------------------------- reindexing


async def test_reindex_window_preserves_straddling_chunk(store, embedder):
    # A window chunk spanning 100..200; reindexing a narrow slice in the middle
    # must not regenerate it from only that slice.
    for ts, text in [(100, "start"), (150, "middle"), (200, "end")]:
        await ingest.handle_new_message(store, embedder, CH, human(ts, text))
    await ingest.reindex_window(store, embedder, CH, 149, 151)

    ids, _ = await store.embeddings_for_channel(CH)
    chunks = await store.chunks_by_id(ids)
    all_text = " ".join(c["text"] for c in chunks.values())
    for word in ("start", "middle", "end"):
        assert word in all_text


async def test_reindex_channel_is_idempotent(store, embedder):
    for ts, text in [(100, "alpha"), (5000, "beta")]:
        await ingest.handle_new_message(store, embedder, CH, human(ts, text))
    first = await ingest.reindex_channel(store, embedder, CH)
    second = await ingest.reindex_channel(store, embedder, CH)
    assert first == second
    ids, mat = await store.embeddings_for_channel(CH)
    assert len(ids) == second
    assert isinstance(mat, np.ndarray)


# ------------------------------------------------------ Slack parameter shape


async def test_backfill_omits_oldest_rather_than_sending_zero(store, embedder):
    # Slack answers oldest="0.000000" with invalid_ts_oldest; "from the
    # beginning" means leaving the parameter off entirely.
    client = FakeClient([human(100, "hello")])
    await ingest.backfill(store, client, embedder, CH)
    assert client.history_kwargs[0]["oldest"] is None


async def test_catch_up_does_send_oldest(store, embedder):
    client = FakeClient([human(100, "first")])
    await ingest.backfill(store, client, embedder, CH)
    client.history_kwargs.clear()
    client._history.append(human(200, "second"))
    await ingest.catch_up(store, client, embedder, CH)
    assert client.history_kwargs[0]["oldest"] == "100.000000"


# --------------------------------------------------- resilience of backfill


class FlakyClient(FakeClient):
    """Fails thread fetches after N successes, like a dropped socket mid-run."""

    def __init__(self, history, replies=None, fail_after=None, fail_times=1):
        super().__init__(history, replies)
        self.fail_after = fail_after
        self.fail_times = fail_times
        self.thread_calls = 0

    async def conversations_replies(self, channel, ts, limit, cursor=None):
        self.thread_calls += 1
        if self.fail_after is not None and self.thread_calls > self.fail_after:
            if self.fail_times > 0:
                self.fail_times -= 1
                raise TimeoutError("socket timed out")
        return FakeResponse(messages=self._replies.get(ts, []), response_metadata={})


def threaded(ts, text, n_replies=1, **extra):
    return human(ts, text, reply_count=n_replies, thread_ts=f"{ts:.6f}", **extra)


async def test_top_level_messages_persist_before_threads_are_fetched(store, embedder):
    # The real failure: 4061 messages fetched, thread 26 of 710 timed out, and
    # every single message was discarded.
    root = threaded(100, "root")
    client = FlakyClient([root, human(200, "plain")], fail_after=0, fail_times=99)
    with pytest.raises(TimeoutError):
        await ingest.ingest_range(store, client, CH)
    stored = await store.messages_in_range(CH, 0, 1e12)
    assert {m.text for m in stored} == {"root", "plain"}


async def test_transient_timeout_is_retried(store, embedder):
    root = threaded(100, "root")
    client = FlakyClient(
        [root],
        replies={"100.000000": [root, human(110, "reply", thread_ts="100.000000")]},
        fail_after=0,
        fail_times=2,  # two failures, then success
    )
    stored, _ = await ingest.ingest_range(store, client, CH)
    assert stored == 2
    texts = {m.text for m in await store.messages_in_range(CH, 0, 1e12)}
    assert texts == {"root", "reply"}


async def test_non_transient_error_still_fails_fast(store, embedder):
    from slack_sdk.errors import SlackApiError

    class Denied(FakeClient):
        async def conversations_history(self, **kw):
            resp = type("R", (), {"status_code": 403, "headers": {}})()
            raise SlackApiError("not_in_channel", resp)

    with pytest.raises(SlackApiError):
        await ingest.ingest_range(store, Denied([]), CH)


async def test_resume_skips_fully_fetched_threads(store, embedder):
    root = threaded(100, "root", n_replies=1)
    replies = {"100.000000": [root, human(110, "reply", thread_ts="100.000000")]}
    await ingest.ingest_range(store, FakeClient([root], replies=replies), CH)

    client2 = FakeClient([root], replies=replies)
    calls_before = client2.history_calls
    await ingest.ingest_range(store, client2, CH)
    assert await store.thread_reply_counts(CH) == {"100.000000": 1}
    assert client2.history_calls == calls_before + 1  # history yes, thread no


async def test_broadcast_reply_does_not_mark_a_thread_complete(store, embedder):
    """A thread_broadcast reply appears in channel history without its siblings.

    Treating that as "already fetched" would permanently lose the rest of the
    thread, which is exactly what a set-of-seen-roots check would have done.
    """
    root = threaded(100, "root", n_replies=3)
    broadcast = human(110, "broadcast reply", thread_ts="100.000000")
    replies = {
        "100.000000": [
            root,
            broadcast,
            human(120, "second reply", thread_ts="100.000000"),
            human(130, "third reply", thread_ts="100.000000"),
        ]
    }
    # History returns the root plus the broadcast, as Slack really does.
    client = FakeClient([root, broadcast], replies=replies)
    await ingest.ingest_range(store, client, CH)

    texts = {m.text for m in await store.messages_in_range(CH, 0, 1e12)}
    assert "second reply" in texts and "third reply" in texts


async def test_thread_reply_counts_ignores_bare_roots(store, embedder):
    from slackqa.store import Message

    await store.upsert_messages(
        [Message(CH, "100.000000", "100.000000", "U1", "root only")]
    )
    assert await store.thread_reply_counts(CH) == {}

