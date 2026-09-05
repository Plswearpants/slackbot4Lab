"""Getting Slack messages into the index and keeping them there truthfully.

Three entry points, in decreasing order of how much work they do:

* :func:`backfill` — one-time paginated pull of a channel's whole history.
* :func:`catch_up` — everything since the last message we stored.
* :func:`reconcile_deletions` — diff a trailing window against Slack to find
  messages deleted while we were not running.

The last one exists because Slack does not replay events missed during
downtime. A ``message_deleted`` that fires while the process is off is simply
lost, so an index that trusted events alone would keep content the workspace
believes it deleted — which matters most in exactly the case deletion is used
for, an accidentally pasted credential.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any

from slackqa.chunker import DEFAULT_GAP_SECONDS, affected_window, build_chunks
from slackqa.embeddings import Embedder
from slackqa.filters import is_indexable
from slackqa.store import Message, Store

logger = logging.getLogger(__name__)

# Internal (non-distributed) Slack apps may request up to 1000 messages per
# call. Distributing the app publicly drops this to 15 and makes backfill
# impractical — see SPEC.md §3.
PAGE_SIZE = 1000

# Courtesy pause between pages. Internal apps get 50+ req/min; this keeps us
# comfortably under that without making backfill slow.
PAGE_PAUSE_SECONDS = 1.2

# Persist thread replies this often, so an interrupted backfill can resume
# rather than restart.
FLUSH_EVERY_THREADS = 25


def _to_message(channel_id: str, ev: Mapping[str, Any]) -> Message:
    thread_ts = ev.get("thread_ts")
    # Slack sets thread_ts == ts on a root once it has replies. Keeping that as
    # the thread key is what lets root and replies group into one chunk.
    return Message(
        channel_id=channel_id,
        ts=str(ev["ts"]),
        thread_ts=str(thread_ts) if thread_ts else None,
        user_id=str(ev.get("user") or ""),
        text=str(ev.get("text") or ""),
    )


async def _call_with_retry(fn, attempts: int = 6, **kwargs) -> Any:
    """Call a Slack Web API method, surviving rate limits and flaky networks.

    Retrying only on 429 was not enough: a backfill makes hundreds of sequential
    calls over many minutes, and a single transient socket timeout killed a
    17-minute run. Network errors are retried with exponential backoff; API
    errors that are not rate limits still fail fast, since retrying a bad token
    or a missing channel just wastes time.
    """
    import aiohttp
    from slack_sdk.errors import SlackApiError

    transient = (TimeoutError, aiohttp.ClientError, ConnectionError, OSError)
    backoff = 2.0

    for attempt in range(attempts):
        try:
            return await fn(**kwargs)
        except SlackApiError as e:
            if e.response.status_code == 429:
                delay = int(e.response.headers.get("Retry-After", 5))
                logger.warning("Rate limited; sleeping %ss", delay)
                await asyncio.sleep(delay)
                continue
            raise
        except transient as e:
            if attempt == attempts - 1:
                raise
            logger.warning(
                "Transient network error (%s); retry %d/%d in %.0fs",
                type(e).__name__, attempt + 1, attempts - 1, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
    raise RuntimeError(f"Slack call failed after {attempts} attempts: {fn}")


# --------------------------------------------------------------------- indexing


async def reindex_window(
    store: Store,
    embedder: Embedder,
    channel_id: str,
    lo: float,
    hi: float,
    *,
    names: Mapping[str, str] | None = None,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
    papers: Mapping[str, str] | None = None,
) -> int:
    """Rebuild chunks and embeddings covering ``[lo, hi]``.

    The range is widened twice before anything is deleted: once to the full span
    of the chunks that overlap it, and once more to cover whole threads that
    reach into it. Rebuilding a narrower range than the chunks being replaced
    would silently drop the parts that fell outside.
    """
    span = await store.chunk_span_overlapping(channel_id, lo, hi)
    if span:
        lo, hi = min(lo, span[0]), max(hi, span[1])

    messages = await store.messages_in_range(channel_id, lo, hi)

    # Pull in the remainder of any thread that reaches into the window.
    seen = {m.ts for m in messages}
    for root in await store.thread_ts_touching(channel_id, lo, hi):
        for m in await store.messages_in_thread(channel_id, root):
            if m.ts not in seen:
                messages.append(m)
                seen.add(m.ts)
                lo, hi = min(lo, m.ts_num), max(hi, m.ts_num)

    await store.delete_chunks_in_range(channel_id, lo, hi)

    chunks = build_chunks(
        messages, gap_seconds=gap_seconds, names=names, papers=papers
    )
    if not chunks:
        return 0

    vecs = embedder.embed_documents([c.text for c in chunks])
    await store.insert_chunks(chunks, embeddings=[v.tolist() for v in vecs])
    return len(chunks)


async def reindex_channel(
    store: Store,
    embedder: Embedder,
    channel_id: str,
    *,
    names: Mapping[str, str] | None = None,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
) -> int:
    messages = await store.messages_in_range(channel_id, 0, float("inf"))
    await store.delete_chunks_in_range(channel_id, 0, float("inf"))
    papers = await store.resolved_papers(channel_id)
    if papers:
        logger.info("Enriching %s with %d resolved papers", channel_id, len(papers))
    chunks = build_chunks(
        messages, gap_seconds=gap_seconds, names=names, papers=papers
    )
    if not chunks:
        return 0
    vecs = embedder.embed_documents([c.text for c in chunks])
    await store.insert_chunks(chunks, embeddings=[v.tolist() for v in vecs])
    logger.info("Reindexed channel=%s chunks=%d", channel_id, len(chunks))
    return len(chunks)


# ---------------------------------------------------------------------- ingest


async def _store_events(
    store: Store,
    channel_id: str,
    events: Sequence[Mapping[str, Any]],
    bot_user_id: str | None,
) -> tuple[int, float]:
    """Filter to indexable messages, persist, return (count, max_ts)."""
    msgs = [_to_message(channel_id, ev) for ev in events if is_indexable(ev, bot_user_id)]
    # Watermark advances past everything seen, indexable or not; otherwise
    # catch_up re-fetches the same bot spam on every start.
    max_ts = max((float(ev["ts"]) for ev in events if ev.get("ts")), default=0.0)
    if msgs:
        await store.upsert_messages(msgs)
    return len(msgs), max_ts


async def fetch_history(
    client, channel_id: str, *, oldest: float = 0.0, limit: int = PAGE_SIZE
) -> list[dict[str, Any]]:
    """All messages newer than ``oldest``, following pagination cursors."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"channel": channel_id, "limit": limit}
        # Slack rejects oldest="0.000000" with invalid_ts_oldest — "from the
        # beginning" means omitting the parameter, not passing zero.
        if oldest > 0:
            kwargs["oldest"] = f"{oldest:.6f}"
        if cursor:
            kwargs["cursor"] = cursor
        resp = await _call_with_retry(client.conversations_history, **kwargs)
        out.extend(resp.get("messages") or [])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        await asyncio.sleep(PAGE_PAUSE_SECONDS)
    return out


async def fetch_thread(client, channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"channel": channel_id, "ts": thread_ts, "limit": PAGE_SIZE}
        if cursor:
            kwargs["cursor"] = cursor
        resp = await _call_with_retry(client.conversations_replies, **kwargs)
        out.extend(resp.get("messages") or [])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        await asyncio.sleep(PAGE_PAUSE_SECONDS)
    return out


async def ingest_range(
    store: Store,
    client,
    channel_id: str,
    *,
    oldest: float = 0.0,
    bot_user_id: str | None = None,
) -> tuple[int, float]:
    """Fetch history (plus thread replies) since ``oldest`` and persist it."""
    top_level = await fetch_history(client, channel_id, oldest=oldest)

    # Persist top-level messages before touching threads. A backfill can run for
    # a quarter of an hour, and losing all of it to one socket timeout near the
    # end is not an acceptable failure mode.
    stored, max_ts = await _store_events(store, channel_id, top_level, bot_user_id)

    threaded = [ev for ev in top_level if ev.get("reply_count")]
    have = await store.thread_reply_counts(channel_id)
    # Slack's reply_count excludes the root, as does our stored count. A thread
    # whose replies were all filtered out (bot-authored) will be re-fetched;
    # that is cheap next to silently keeping a half-fetched thread.
    pending = [
        ev
        for ev in threaded
        if have.get(str(ev["ts"]), 0) < int(ev.get("reply_count") or 0)
    ]
    logger.info(
        "channel=%s: %d top-level messages, %d threads (%d already fetched) "
        "(~%.0fs at %.1fs/req)",
        channel_id,
        len(top_level),
        len(threaded),
        len(threaded) - len(pending),
        len(pending) * PAGE_PAUSE_SECONDS,
        PAGE_PAUSE_SECONDS,
    )

    batch: list[Mapping[str, Any]] = []
    for i, ev in enumerate(pending, start=1):
        replies = await fetch_thread(client, channel_id, str(ev["ts"]))
        # conversations.replies echoes the root; skip it to avoid a redundant
        # upsert of a row we already have.
        batch.extend(r for r in replies if str(r.get("ts")) != str(ev["ts"]))

        if i % FLUSH_EVERY_THREADS == 0 or i == len(pending):
            n, ts = await _store_events(store, channel_id, batch, bot_user_id)
            stored += n
            max_ts = max(max_ts, ts)
            batch = []
            logger.info("  threads %d/%d (%d messages stored)", i, len(pending), stored)

        if i < len(pending):
            # One request per thread is unavoidable, and this pacing keeps us
            # at Slack's ~50 req/min for internal apps. Backfill is one-time.
            await asyncio.sleep(PAGE_PAUSE_SECONDS)

    if max_ts:
        await store.set_last_ts(channel_id, max_ts)
    return stored, max_ts


async def backfill(
    store: Store,
    client,
    embedder: Embedder,
    channel_id: str,
    *,
    bot_user_id: str | None = None,
    names: Mapping[str, str] | None = None,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
) -> int:
    logger.info("Backfilling channel=%s", channel_id)
    stored, _ = await ingest_range(
        store, client, channel_id, oldest=0.0, bot_user_id=bot_user_id
    )
    await reindex_channel(
        store, embedder, channel_id, names=names, gap_seconds=gap_seconds
    )
    await store.mark_backfilled(channel_id, time.time())
    logger.info("Backfill complete channel=%s messages=%d", channel_id, stored)
    return stored


async def catch_up(
    store: Store,
    client,
    embedder: Embedder,
    channel_id: str,
    *,
    bot_user_id: str | None = None,
    names: Mapping[str, str] | None = None,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
) -> int:
    """Ingest whatever arrived while the process was down."""
    last = await store.get_last_ts(channel_id)
    stored, max_ts = await ingest_range(
        store, client, channel_id, oldest=last, bot_user_id=bot_user_id
    )
    if stored:
        await reindex_window(
            store,
            embedder,
            channel_id,
            last,
            max_ts,
            names=names,
            gap_seconds=gap_seconds,
        )
    logger.info("Catch-up channel=%s new=%d", channel_id, stored)
    return stored


async def reconcile_deletions(
    store: Store,
    client,
    embedder: Embedder,
    channel_id: str,
    *,
    window_days: int = 30,
    names: Mapping[str, str] | None = None,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
) -> int:
    """Purge messages we hold that Slack no longer returns.

    ``conversations.history`` omits deleted messages, so anything stored in the
    window but absent upstream was deleted while we were not listening.
    """
    since = time.time() - window_days * 86400
    upstream = await fetch_history(client, channel_id, oldest=since)
    upstream_ts = {str(ev["ts"]) for ev in upstream}
    for ev in upstream:
        if ev.get("reply_count"):
            replies = await fetch_thread(client, channel_id, str(ev["ts"]))
            upstream_ts.update(str(r["ts"]) for r in replies)

    stored_ts = await store.message_ts_since(channel_id, since)
    vanished = sorted(stored_ts - upstream_ts)
    if not vanished:
        return 0

    logger.info("Purging %d deleted messages from channel=%s", len(vanished), channel_id)
    await store.delete_messages(channel_id, vanished)

    lo = min(float(t) for t in vanished)
    hi = max(float(t) for t in vanished)
    await reindex_window(
        store,
        embedder,
        channel_id,
        *affected_window(lo, gap_seconds),
        names=names,
        gap_seconds=gap_seconds,
    )
    if hi != lo:
        await reindex_window(
            store,
            embedder,
            channel_id,
            *affected_window(hi, gap_seconds),
            names=names,
            gap_seconds=gap_seconds,
        )
    return len(vanished)


# ----------------------------------------------------------------- live events


async def handle_new_message(
    store: Store,
    embedder: Embedder,
    channel_id: str,
    event: Mapping[str, Any],
    *,
    bot_user_id: str | None = None,
    names: Mapping[str, str] | None = None,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
) -> bool:
    if not is_indexable(event, bot_user_id):
        return False
    m = _to_message(channel_id, event)
    await store.upsert_messages([m])
    await store.set_last_ts(channel_id, m.ts_num)
    await reindex_window(
        store,
        embedder,
        channel_id,
        *affected_window(m.ts_num, gap_seconds),
        names=names,
        gap_seconds=gap_seconds,
    )
    return True


async def handle_edit(
    store: Store,
    embedder: Embedder,
    channel_id: str,
    event: Mapping[str, Any],
    *,
    bot_user_id: str | None = None,
    names: Mapping[str, str] | None = None,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
) -> bool:
    """Handle ``message_changed``; the edited message is nested under 'message'."""
    inner = event.get("message") or {}
    if not inner.get("ts"):
        return False
    if not is_indexable(inner, bot_user_id):
        # An edit can make a message non-indexable; treat that as a deletion.
        return await handle_delete(
            store,
            embedder,
            channel_id,
            {"deleted_ts": inner["ts"]},
            names=names,
            gap_seconds=gap_seconds,
        )
    m = _to_message(channel_id, inner)
    await store.upsert_messages([m])
    await reindex_window(
        store,
        embedder,
        channel_id,
        *affected_window(m.ts_num, gap_seconds),
        names=names,
        gap_seconds=gap_seconds,
    )
    return True


async def handle_delete(
    store: Store,
    embedder: Embedder,
    channel_id: str,
    event: Mapping[str, Any],
    *,
    names: Mapping[str, str] | None = None,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
) -> bool:
    ts = event.get("deleted_ts") or (event.get("previous_message") or {}).get("ts")
    if not ts:
        return False
    await store.delete_message(channel_id, str(ts))
    await reindex_window(
        store,
        embedder,
        channel_id,
        *affected_window(float(ts), gap_seconds),
        names=names,
        gap_seconds=gap_seconds,
    )
    return True
