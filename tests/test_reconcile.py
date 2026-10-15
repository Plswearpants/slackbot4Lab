"""Bring the index in line with Slack as it reads today.

The principle is provenance: everything indexed must be something a citation can
point back to. So an edited message is indexed in its edited form, and anything
deleted in Slack leaves the index however old it is.

This was needed because `message.channels` was never subscribed. Edits were
never picked up, and deletions were caught only by a 30-day reconcile at
startup — which also had its own bug, rebuilding chunks only around the
earliest and latest deletion and leaving deleted text in between.
"""

from __future__ import annotations

import pytest

from slackqa import ingest

CH = "C0TEST"
BOT = "U0BOT"
DAY = 86400.0
NOW = 1_800_000_000.0  # a fixed "today" well after everything below


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    monkeypatch.setattr(ingest, "PAGE_PAUSE_SECONDS", 0)

    async def instant(_seconds):
        return None

    monkeypatch.setattr(ingest.asyncio, "sleep", instant)


class Resp(dict):
    pass


class Slack:
    """A channel whose contents can change between calls.

    Supports the latest/oldest/inclusive parameters used to re-check a single
    message, which the plain backfill fake does not need.
    """

    def __init__(self, messages):
        self.msgs = {m["ts"]: dict(m) for m in messages}

    def _top(self):
        return [m for m in self.msgs.values()
                if not m.get("thread_ts") or m["thread_ts"] == m["ts"]]

    def _thread(self, root):
        return [m for m in self.msgs.values() if m.get("thread_ts") == root or m["ts"] == root]

    @staticmethod
    def _window(msgs, oldest, latest, inclusive):
        out = []
        for m in msgs:
            t = float(m["ts"])
            lo = float(oldest) if oldest else float("-inf")
            hi = float(latest) if latest else float("inf")
            ok = (lo <= t <= hi) if inclusive else (lo < t < hi)
            if ok:
                out.append(m)
        return sorted(out, key=lambda m: float(m["ts"]))

    async def conversations_history(self, channel, limit, oldest=None, latest=None,
                                    inclusive=False, cursor=None):
        top = []
        for m in self._top():
            m = dict(m)
            replies = [r for r in self._thread(m["ts"]) if r["ts"] != m["ts"]]
            if replies:
                m["reply_count"] = len(replies)
                m["thread_ts"] = m["ts"]
            top.append(m)
        if oldest is not None and latest is None and not inclusive:
            msgs = [m for m in top if float(m["ts"]) > float(oldest)]
        else:
            msgs = self._window(top, oldest, latest, inclusive)
        return Resp(messages=sorted(msgs, key=lambda m: float(m["ts"]))[:limit],
                    response_metadata={})

    async def conversations_replies(self, channel, ts, limit, oldest=None, latest=None,
                                    inclusive=False, cursor=None):
        if ts not in self.msgs:
            raise RuntimeError("thread_not_found")
        msgs = self._thread(ts)
        if oldest is not None or latest is not None:
            msgs = self._window(msgs, oldest, latest, inclusive)
        return Resp(messages=sorted(msgs, key=lambda m: float(m["ts"]))[:limit],
                    response_metadata={})

    # mutations
    def edit(self, ts, text):
        self.msgs[ts]["text"] = text

    def delete(self, ts):
        del self.msgs[ts]

    def post(self, m):
        self.msgs[m["ts"]] = dict(m)


def msg(t, text, user="U1", thread=None):
    m = {"ts": f"{t:.6f}", "user": user, "text": text}
    if thread is not None:
        m["thread_ts"] = f"{thread:.6f}"
    return m


async def texts(store):
    rows = await store.messages_in_range(CH, 0, float("inf"))
    return {m.text for m in rows}


async def chunk_text(store):
    """Everything searchable: the text of every chunk in the channel."""
    ids, _ = await store.embeddings_for_channel(CH)
    return " ".join(c["text"] for c in (await store.chunks_by_id(ids)).values())


async def seed(store, embedder, slack):
    await ingest.backfill(store, slack, embedder, CH, bot_user_id=BOT)


async def reconcile(store, embedder, slack, taken_at=NOW):
    plan = await ingest.plan_reconcile(store, slack, CH, bot_user_id=BOT, taken_at=taken_at)
    counts = await ingest.apply_plan(store, slack, embedder, plan, bot_user_id=BOT)
    return plan, counts


# ------------------------------------------------- the provenance principle


async def test_an_edited_message_is_indexed_as_edited(store, embedder):
    slack = Slack([msg(NOW - 90 * DAY, "the cryostat holds 4.2 K")])
    await seed(store, embedder, slack)
    slack.edit(f"{NOW - 90 * DAY:.6f}", "the cryostat holds 1.5 K after the upgrade")

    _, counts = await reconcile(store, embedder, slack)

    assert counts["edited"] == 1
    assert await texts(store) == {"the cryostat holds 1.5 K after the upgrade"}
    body = await chunk_text(store)
    assert "1.5 K" in body and "4.2 K" not in body


async def test_a_message_deleted_long_ago_is_removed(store, embedder):
    """The 30-day startup reconcile can never see this one."""
    old = NOW - 200 * DAY
    slack = Slack([msg(old, "password is hunter2"), msg(old + 30, "thanks")])
    await seed(store, embedder, slack)
    slack.delete(f"{old:.6f}")

    _, counts = await reconcile(store, embedder, slack)

    assert counts["deleted"] == 1
    assert "password is hunter2" not in await texts(store)
    assert "hunter2" not in await chunk_text(store)


async def test_scattered_deletions_leave_no_text_behind(store, embedder):
    """Rebuilding only around the earliest and latest deletion left the
    middle one's text in its chunk — searchable and quotable."""
    base = NOW - 100 * DAY
    rows = [msg(base + i * 10 * DAY, f"note {i} zz{i}") for i in range(5)]
    slack = Slack(rows)
    await seed(store, embedder, slack)
    for i in (0, 2, 4):
        slack.delete(rows[i]["ts"])

    await reconcile(store, embedder, slack)

    body = await chunk_text(store)
    for gone in ("zz0", "zz2", "zz4"):
        assert gone not in body, gone
    assert "zz1" in body and "zz3" in body


async def test_the_startup_reconcile_no_longer_leaves_middle_deletions(
    store, embedder, monkeypatch
):
    """Same bug, in the existing 30-day path."""
    base = NOW - 20 * DAY
    rows = [msg(base + i * 3 * DAY, f"entry zz{i}") for i in range(5)]
    slack = Slack(rows)
    await seed(store, embedder, slack)
    for i in (0, 2, 4):
        slack.delete(rows[i]["ts"])

    monkeypatch.setattr(ingest.time, "time", lambda: NOW)
    await ingest.reconcile_deletions(store, slack, embedder, CH, window_days=30)

    body = await chunk_text(store)
    assert "zz2" not in body


async def test_a_late_reply_to_an_old_thread_is_added(store, embedder):
    """Catch-up only follows threads whose root is new, so a reply to an old
    thread was only ever visible as a live event — which never came."""
    root = NOW - 150 * DAY
    slack = Slack([msg(root, "tip keeps crashing", thread=root)])
    await seed(store, embedder, slack)
    slack.post(msg(NOW - 2 * DAY, "fixed: it was the coarse motor", thread=root))

    _, counts = await reconcile(store, embedder, slack)

    assert counts["added"] == 1
    assert "fixed: it was the coarse motor" in await texts(store)


async def test_an_edit_that_makes_a_message_unindexable_removes_it(store, embedder):
    """Edited into a question for the bot: a query, not content."""
    t = NOW - 60 * DAY
    slack = Slack([msg(t, "the leak was at the gate valve")])
    await seed(store, embedder, slack)
    slack.edit(f"{t:.6f}", f"<@{BOT}> where was the leak?")

    _, counts = await reconcile(store, embedder, slack)

    assert counts["deleted"] == 1
    assert await texts(store) == set()


# ----------------------------------------------- safety between plan and apply


async def test_messages_newer_than_the_plan_are_never_deleted(store, embedder):
    """Planning takes an hour with the bot live. Anything it indexed in that
    hour is absent from the plan's snapshot and must not look deleted."""
    t = NOW - 10 * DAY
    slack = Slack([msg(t, "older message")])
    await seed(store, embedder, slack)
    # Indexed live after the snapshot began, so absent from what was fetched.
    late = msg(NOW + 600, "posted while planning")
    await ingest.handle_new_message(store, embedder, CH, late, bot_user_id=BOT)

    plan = await ingest.plan_reconcile(store, slack, CH, bot_user_id=BOT, taken_at=NOW)
    assert late["ts"] not in plan.deleted
    await ingest.apply_plan(store, slack, embedder, plan, bot_user_id=BOT)

    assert "posted while planning" in await texts(store)


async def test_apply_uses_slack_as_it_is_now_not_as_it_was_planned(store, embedder):
    """An edit between planning and applying must not be reverted."""
    t = NOW - 40 * DAY
    slack = Slack([msg(t, "first version")])
    await seed(store, embedder, slack)
    slack.edit(f"{t:.6f}", "second version")
    plan = await ingest.plan_reconcile(store, slack, CH, bot_user_id=BOT, taken_at=NOW)

    slack.edit(f"{t:.6f}", "third version")  # edited again before apply
    await ingest.apply_plan(store, slack, embedder, plan, bot_user_id=BOT)

    assert await texts(store) == {"third version"}


async def test_a_planned_deletion_is_rechecked_before_removing(store, embedder):
    t = NOW - 40 * DAY
    slack = Slack([msg(t, "still here")])
    await seed(store, embedder, slack)
    plan = ingest.ReconcilePlan(channel_id=CH, taken_at=NOW,
                                deleted=[f"{t:.6f}"], edited={}, missing=[])

    counts = await ingest.apply_plan(store, slack, embedder, plan, bot_user_id=BOT)

    assert counts["deleted"] == 0
    assert "still here" in await texts(store)


async def test_a_reply_in_a_deleted_thread_is_treated_as_deleted(store, embedder):
    root = NOW - 80 * DAY
    slack = Slack([msg(root, "root", thread=root), msg(root + 60, "a reply", thread=root)])
    await seed(store, embedder, slack)
    slack.delete(f"{root + 60:.6f}")
    slack.delete(f"{root:.6f}")  # whole thread gone; replies() now errors

    await reconcile(store, embedder, slack)

    assert await texts(store) == set()


async def test_nothing_changed_means_nothing_planned(store, embedder):
    slack = Slack([msg(NOW - 5 * DAY, "steady"), msg(NOW - 4 * DAY, "state")])
    await seed(store, embedder, slack)
    plan, counts = await reconcile(store, embedder, slack)
    assert (plan.deleted, plan.edited, plan.missing) == ([], {}, [])
    assert counts == {"deleted": 0, "edited": 0, "added": 0}


async def test_a_plan_survives_being_written_to_disk(tmp_path, store, embedder):
    t = NOW - 30 * DAY
    slack = Slack([msg(t, "before")])
    await seed(store, embedder, slack)
    slack.edit(f"{t:.6f}", "after")
    plan = await ingest.plan_reconcile(store, slack, CH, bot_user_id=BOT, taken_at=NOW)

    path = tmp_path / "plan.json"
    plan.save(path)
    again = ingest.ReconcilePlan.load(path)

    assert again == plan


# --------------------------------------- paper enrichment survives live edits


async def test_a_live_message_keeps_paper_titles_in_its_chunk(store, embedder):
    """#coolpapers chunks carry each linked paper's title and abstract, since a
    bare URL is unsearchable. Live rebuilds passed no paper metadata, so the
    first live message in a window stripped it — harmless while live events
    never arrived, and immediate once they did."""
    doi = "10.1103/PhysRevB.108.094505"
    await store.record_reference(doi, CH, "indexed",
                                 title="Charge order in a kagome superconductor")
    t = NOW - DAY
    await ingest.handle_new_message(
        store, embedder, CH, msg(t, f"worth a read https://doi.org/{doi}"), bot_user_id=BOT
    )
    assert "kagome superconductor" in await chunk_text(store)


# ------------------------------------------------------- pacing and patience


async def test_planning_pauses_between_threads_like_backfill(store, embedder, monkeypatch):
    """Planning fetched thousands of threads back to back and was
    rate-limited within a minute of starting."""
    root = NOW - 50 * DAY
    rows = []
    for i in range(5):
        r = root + i * DAY
        rows += [msg(r, f"root {i}", thread=r), msg(r + 60, f"reply {i}", thread=r)]
    slack = Slack(rows)
    await seed(store, embedder, slack)

    pauses = []

    async def record(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(ingest, "PAGE_PAUSE_SECONDS", 1.2)
    monkeypatch.setattr(ingest.asyncio, "sleep", record)
    await ingest.plan_reconcile(store, slack, CH, bot_user_id=BOT, taken_at=NOW)
    assert pauses.count(1.2) >= 5


async def test_rate_limits_do_not_use_up_the_retry_budget(monkeypatch):
    """Six 429s in a row on one call used to abort an hour-long run."""
    from slack_sdk.errors import SlackApiError

    class R:
        status_code = 429
        headers = {"Retry-After": "1"}

        def get(self, *_):
            return None

    calls = {"n": 0}

    async def flaky(**kw):
        calls["n"] += 1
        if calls["n"] <= 9:
            raise SlackApiError("ratelimited", R())
        return {"ok": True}

    assert await ingest._call_with_retry(flaky, attempts=6) == {"ok": True}
    assert calls["n"] == 10
