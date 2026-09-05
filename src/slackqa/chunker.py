"""Group messages into retrieval units.

A single Slack message is a poor retrieval unit: "yeah that works", "+1", "see
above" carry no standalone meaning, so retrieving them individually hands the
model fragments and lets it invent the connective tissue. Chunks here are
conversation-sized:

* **thread** — a thread root plus all its replies.
* **window** — contiguous unthreaded messages, split wherever the gap between
  consecutive messages exceeds ``gap_seconds``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from slackqa.store import Chunk, Message

DEFAULT_GAP_SECONDS = 600


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M")


def render(messages: Sequence[Message], names: Mapping[str, str] | None = None) -> str:
    """Render messages as readable transcript lines.

    Real timestamps and display names, because both the BM25 index and the model
    read this text — slicing a raw Slack epoch string yields noise like
    ``[1700000000.00000 @alice]`` that helps neither.
    """
    names = names or {}
    lines = []
    for m in messages:
        who = names.get(m.user_id, m.user_id)
        lines.append(f"[{_fmt_time(m.ts_num)}] {who}: {m.text}")
    return "\n".join(lines)


def _make_chunk(
    messages: Sequence[Message],
    kind: str,
    names: Mapping[str, str] | None,
    anchor_ts: str | None = None,
) -> Chunk:
    ordered = sorted(messages, key=lambda m: m.ts_num)
    participants: list[str] = []
    for m in ordered:
        if m.user_id not in participants:
            participants.append(m.user_id)
    return Chunk(
        channel_id=ordered[0].channel_id,
        kind=kind,
        anchor_ts=anchor_ts or ordered[0].ts,
        start_ts=ordered[0].ts_num,
        end_ts=ordered[-1].ts_num,
        participants=participants,
        msg_count=len(ordered),
        text=render(ordered, names),
    )


def enrich_with_papers(text: str, papers: Mapping[str, str]) -> str:
    """Append what each linked paper is about.

    A link-sharing channel indexes as a wall of URLs: the chunk holds
    "nature.com/articles/s41567-023-02294-y" while every word someone would
    search for lives in the paper behind it. Folding in the resolved title and
    abstract is what makes such a channel searchable at all.
    """
    if not papers:
        return text
    from slackqa.literature import extract

    blurbs: list[str] = []
    for ref in extract(text):
        blurb = papers.get(ref.identity)
        if blurb and blurb not in blurbs:
            blurbs.append(blurb)
    if not blurbs:
        return text
    return text + "\n\n" + "\n".join(f"[paper] {b}" for b in blurbs)


def build_chunks(
    messages: Iterable[Message],
    *,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
    names: Mapping[str, str] | None = None,
    papers: Mapping[str, str] | None = None,
) -> list[Chunk]:
    """Build retrieval chunks from a channel's messages.

    Threads become one chunk each regardless of how long they span; everything
    else is grouped by time proximity. Returned in chronological order of first
    message.
    """
    msgs = sorted(messages, key=lambda m: (m.ts_num, m.ts))
    if not msgs:
        return []

    threads: dict[str, list[Message]] = defaultdict(list)
    flat: list[Message] = []

    for m in msgs:
        if m.thread_ts:
            threads[m.thread_ts].append(m)
        else:
            flat.append(m)

    chunks: list[Chunk] = []

    # Thread chunks anchor on the thread root so the permalink opens the thread.
    for thread_ts, group in threads.items():
        chunks.append(_make_chunk(group, "thread", names, anchor_ts=thread_ts))

    # Window chunks: split on gaps.
    window: list[Message] = []
    for m in flat:
        if window and (m.ts_num - window[-1].ts_num) > gap_seconds:
            chunks.append(_make_chunk(window, "window", names))
            window = []
        window.append(m)
    if window:
        chunks.append(_make_chunk(window, "window", names))

    if papers:
        chunks = [
            replace(c, text=enrich_with_papers(c.text, papers)) for c in chunks
        ]

    chunks.sort(key=lambda c: (c.start_ts, c.anchor_ts))
    return chunks


def affected_window(ts: float, gap_seconds: int = DEFAULT_GAP_SECONDS) -> tuple[float, float]:
    """Range to rebuild when the message at ``ts`` is edited or deleted.

    Padded by one gap on each side: removing a message can merge the windows
    that sat either side of it, so rebuilding only the exact timestamp would
    leave the neighbours wrongly split.
    """
    return ts - gap_seconds, ts + gap_seconds
