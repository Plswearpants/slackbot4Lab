from __future__ import annotations

from slackqa.chunker import affected_window, build_chunks, render
from slackqa.filters import is_indexable, mentioned_users, strip_mentions
from slackqa.store import Message

CH = "C0TEST"
BOT = "U0BOT"


def msg(ts: float, text: str, user: str = "U1", thread_ts: str | None = None) -> Message:
    return Message(CH, f"{ts:.6f}", thread_ts, user, text)


# ------------------------------------------------------------------- chunking


def test_empty_input():
    assert build_chunks([]) == []


def test_contiguous_messages_form_one_window():
    msgs = [msg(100, "a"), msg(200, "b"), msg(300, "c")]
    chunks = build_chunks(msgs, gap_seconds=600)
    assert len(chunks) == 1
    assert chunks[0].msg_count == 3
    assert chunks[0].kind == "window"


def test_gap_splits_windows():
    msgs = [msg(100, "a"), msg(200, "b"), msg(5000, "c")]
    chunks = build_chunks(msgs, gap_seconds=600)
    assert len(chunks) == 2
    assert [c.msg_count for c in chunks] == [2, 1]


def test_gap_boundary_is_exclusive():
    # Exactly gap_seconds apart stays in the same window; one second more splits.
    assert len(build_chunks([msg(100, "a"), msg(700, "b")], gap_seconds=600)) == 1
    assert len(build_chunks([msg(100, "a"), msg(701, "b")], gap_seconds=600)) == 2


def test_thread_becomes_one_chunk():
    msgs = [
        msg(100, "root", thread_ts="100.000000"),
        msg(110, "reply", thread_ts="100.000000"),
        msg(9999, "late reply", thread_ts="100.000000"),
    ]
    chunks = build_chunks(msgs, gap_seconds=600)
    assert len(chunks) == 1
    assert chunks[0].kind == "thread"
    assert chunks[0].msg_count == 3


def test_thread_anchors_on_root_ts():
    msgs = [
        msg(100, "root", thread_ts="100.000000"),
        msg(110, "reply", thread_ts="100.000000"),
    ]
    assert build_chunks(msgs)[0].anchor_ts == "100.000000"


def test_threads_and_windows_coexist():
    msgs = [
        msg(100, "chat a"),
        msg(150, "chat b"),
        msg(200, "root", thread_ts="200.000000"),
        msg(220, "reply", thread_ts="200.000000"),
        msg(9000, "later chat"),
    ]
    chunks = build_chunks(msgs, gap_seconds=600)
    kinds = sorted(c.kind for c in chunks)
    assert kinds == ["thread", "window", "window"]


def test_chunks_are_chronological():
    msgs = [msg(9000, "late"), msg(100, "early"), msg(5000, "mid")]
    chunks = build_chunks(msgs, gap_seconds=600)
    starts = [c.start_ts for c in chunks]
    assert starts == sorted(starts)


def test_participants_deduped_in_order():
    msgs = [msg(100, "a", user="U1"), msg(150, "b", user="U2"), msg(200, "c", user="U1")]
    assert build_chunks(msgs)[0].participants == ["U1", "U2"]


def test_render_uses_names_and_real_timestamps():
    text = render([msg(1700000000, "hello", user="U1")], names={"U1": "alice"})
    assert "alice: hello" in text
    assert "2023-11-14" in text  # real date, not a sliced epoch string
    assert "1700000000" not in text


def test_render_falls_back_to_user_id():
    assert "U9: hi" in render([msg(100, "hi", user="U9")])


def test_affected_window_pads_both_sides():
    lo, hi = affected_window(1000.0, gap_seconds=600)
    assert lo == 400.0 and hi == 1600.0


# -------------------------------------------------------------------- filters


def test_excludes_system_subtypes():
    assert not is_indexable({"subtype": "channel_join", "user": "U1", "text": "joined"})


def test_excludes_bot_messages():
    assert not is_indexable({"bot_id": "B1", "user": "U1", "text": "build passed"})
    assert not is_indexable({"subtype": "bot_message", "text": "deploy ok"})


def test_excludes_our_own_messages():
    assert not is_indexable({"user": BOT, "text": "here is my answer"}, bot_user_id=BOT)


def test_excludes_questions_directed_at_us():
    ev = {"user": "U1", "text": f"<@{BOT}> what did we decide about postgres?"}
    assert not is_indexable(ev, bot_user_id=BOT)


def test_includes_ordinary_human_message():
    assert is_indexable({"user": "U1", "text": "we decided on postgres"}, bot_user_id=BOT)


def test_excludes_empty_text():
    assert not is_indexable({"user": "U1", "text": "   "})


def test_mention_helpers():
    assert mentioned_users("<@U123> and <@W456> hi") == {"U123", "W456"}
    assert strip_mentions("<@U123> what is the plan?") == "what is the plan?"


# --------------------------------------------------- mentions inside a question

NAMES = {"U0BOT": "LAIRbot", "U8JQ4HFV3": "Markus", "USQCN4F9R": "Sarah"}


def test_mention_becomes_a_name_not_a_hole():
    """Stripping every mention erased the subject of the question: "what is
    @Markus working on" became "what is working on" before retrieval ran."""
    from slackqa.filters import name_mentions

    q = "<@U0BOT> what is <@U8JQ4HFV3> working on these days?"
    assert name_mentions(q, NAMES, drop={"U0BOT"}) == (
        "what is Markus working on these days?"
    )


def test_several_people_in_one_question():
    from slackqa.filters import name_mentions

    q = "<@U0BOT> did <@U8JQ4HFV3> and <@USQCN4F9R> agree?"
    assert name_mentions(q, NAMES, drop={"U0BOT"}) == "did Markus and Sarah agree?"


def test_unknown_id_falls_back_to_the_id():
    from slackqa.filters import name_mentions

    out = name_mentions("<@U0BOT> ask <@UNKNOWN1>", NAMES, drop={"U0BOT"})
    assert out == "ask UNKNOWN1" or "UNKNOWN1" in out


def test_question_without_mentions_is_untouched():
    from slackqa.filters import name_mentions

    assert name_mentions("what is the status?", NAMES) == "what is the status?"


def test_naming_a_person_makes_them_searchable():
    # Chunks render as "Markus: ...", so substituting the name is what lets
    # retrieval find that person's messages at all.
    from slackqa.filters import name_mentions
    from slackqa.retrieval import fts_query

    q = name_mentions("<@U0BOT> what did <@U8JQ4HFV3> say", NAMES, drop={"U0BOT"})
    assert '"Markus"' in fts_query(q)


# ------------------------------------------- paper metadata folded into chunks

PAPERS = {
    "10.1038/s41567-023-02294-y": "Superconductivity in a monolayer — We report "
                                  "an unconventional pairing state.",
    "arXiv:2412.02813": "Quasiparticle interference imaging",
}


def test_bare_link_gains_the_papers_words():
    """A link-sharing channel indexes as a wall of URLs: the chunk holds the
    address while every searchable word lives in the paper behind it."""
    from slackqa.chunker import enrich_with_papers

    text = "[2023-12-12 17:34] Markus: <https://doi.org/10.1038/s41567-023-02294-y>"
    out = enrich_with_papers(text, PAPERS)
    assert "Superconductivity in a monolayer" in out
    assert "unconventional pairing" in out
    assert text in out  # the original conversation survives


def test_unresolved_links_leave_the_text_alone():
    from slackqa.chunker import enrich_with_papers

    text = "look at https://example.org/unknown"
    assert enrich_with_papers(text, PAPERS) == text


def test_no_papers_is_a_no_op():
    from slackqa.chunker import enrich_with_papers

    text = "10.1038/s41567-023-02294-y"
    assert enrich_with_papers(text, {}) == text


def test_the_same_paper_twice_is_appended_once():
    from slackqa.chunker import enrich_with_papers

    text = ("10.1038/s41567-023-02294-y and again "
            "https://doi.org/10.1038/s41567-023-02294-y")
    assert enrich_with_papers(text, PAPERS).count("Superconductivity in a monolayer") == 1


def test_build_chunks_enriches():
    msgs = [msg(100, "https://doi.org/10.1038/s41567-023-02294-y")]
    out = build_chunks(msgs, papers=PAPERS)
    assert "Superconductivity in a monolayer" in out[0].text


def test_channel_reference_becomes_a_name():
    # "<#CLXDY4AK1>" reached a question verbatim and matched nothing.
    from slackqa.filters import name_mentions

    assert name_mentions("coaxes for <#CLXDY4AK1|4probe>", {}) == "coaxes for #4probe"
    assert name_mentions("coaxes for <#CLXDY4AK1>", {}, channels={"CLXDY4AK1": "4probe"}) == (
        "coaxes for #4probe"
    )
