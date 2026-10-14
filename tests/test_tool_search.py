"""Model-driven search: the model chooses the query, the person and the window.

The harness only executes what it asks for, inside the channel the question came
from. Born from a real miss: asked "What is yuan doing in the last month?",
retrieval ranked a 2025 conversation and an 82-character password note above
Yuan's Oct 6 experiment update, because nothing in the pipeline knew what
"the last month" meant — the model was not even told today's date.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from slackqa.answerer import NO_ANSWER, SEARCH_TOOL, Answerer, ChatTurn, ToolCall
from slackqa.retrieval import Retriever
from slackqa.store import Chunk

CH = "C0OMICRON"
OTHER = "C0ELSEWHERE"
TEAM = "https://lab.slack.com"
YUAN, SARAH = "U08YUAN", "U0SARAH"


def ts(y, m, d, h=12) -> float:
    return datetime(y, m, d, h).timestamp()


def chunk(text, when, who, channel=CH) -> Chunk:
    return Chunk(
        channel_id=channel,
        kind="window",
        anchor_ts=f"{when:.6f}",
        start_ts=when,
        end_ts=when + 60,
        participants=who,
        msg_count=1,
        text=text,
    )


@pytest.fixture
async def omicron(store, embedder):
    """The shape of the real #omicron around October 2026."""
    await store.cache_user_name(YUAN, "Yuan Xie", 0)
    await store.cache_user_name(SARAH, "Sarah", 0)
    rows = [
        # Old, long, chatty conversations that name Yuan — what retrieval
        # actually ranked first.
        chunk("[2025-05-02] Yuan Xie: helped Vanessa with the bakeout box, "
              "Yuan Xie and Sarah discussed the ion gauge", ts(2025, 5, 2), [YUAN, SARAH]),
        chunk("[2026-07-07] Yuan Xie: all signals from the ion gauge were artifacts",
              ts(2026, 7, 7), [YUAN]),
        # Inside the window.
        chunk("[2026-10-01] Yuan Xie: We lost a sample plate.", ts(2026, 10, 1), [YUAN]),
        chunk("[2026-10-06] Yuan Xie: A quick update on my experiment: unfortunately "
              "disassembling the bakeout box damaged a feedthrough, so the cooldown "
              "is delayed by a week while we replace it.", ts(2026, 10, 6), [YUAN]),
        chunk("[2026-10-13] Yuan Xie: The password expired again.", ts(2026, 10, 13), [YUAN]),
        # Someone else, same window.
        chunk("[2026-10-08] Sarah: ordered new sample plates", ts(2026, 10, 8), [SARAH]),
        # Same person, another channel — must never be reachable from here.
        chunk("[2026-10-09] Yuan Xie: private note in another channel",
              ts(2026, 10, 9), [YUAN], channel=OTHER),
    ]
    vecs = embedder.embed_documents([c.text for c in rows])
    await store.insert_chunks(rows, embeddings=[v.tolist() for v in vecs])
    return Retriever(store, embedder)


# ------------------------------------------------------------ the search tool


async def test_the_yuan_question_finds_the_oct_6_update(omicron):
    """The regression this file exists for."""
    hits = await omicron.search(
        CH, "*", top_k=8, person="yuan",
        since=date(2026, 9, 13), until=date(2026, 10, 13),
    )
    texts = " ".join(h.text for h in hits)
    assert "quick update on my experiment" in texts
    assert "2025-05-02" not in texts and "2026-07-07" not in texts


async def test_a_narrow_filter_returns_everything_in_date_order(omicron):
    """When the filters already define the answer set, ranking on the query text
    can only lose things. "Everything Yuan said since Sep 13" is a fetch."""
    hits = await omicron.search(CH, "*", top_k=8, person="Yuan",
                                since=date(2026, 9, 13), until=date(2026, 10, 13))
    starts = [h.chunk["start_ts"] for h in hits]
    assert starts == sorted(starts)
    assert len(hits) == 3


async def test_person_matches_a_lowercase_fragment_of_the_display_name(omicron):
    hits = await omicron.search(CH, "*", top_k=8, person="yuan",
                                since=date(2026, 10, 1), until=date(2026, 10, 31))
    assert hits and all(YUAN in h.chunk["participants"] for h in hits)


async def test_the_window_alone_excludes_older_history(omicron):
    hits = await omicron.search(CH, "bakeout box", top_k=8,
                                since=date(2026, 9, 13), until=date(2026, 10, 13))
    assert all(h.chunk["start_ts"] >= ts(2026, 9, 13, 0) for h in hits)


async def test_until_is_inclusive_of_the_whole_day(omicron):
    hits = await omicron.search(CH, "*", top_k=8, person="Yuan",
                                since=date(2026, 10, 13), until=date(2026, 10, 13))
    assert any("password expired" in h.text for h in hits)


async def test_another_channel_is_unreachable_whatever_the_filters(omicron):
    """D2 holds inside the tool: the person filter must not widen the scope."""
    hits = await omicron.search(CH, "*", top_k=50, person="Yuan")
    assert all(h.chunk["channel_id"] == CH for h in hits)
    assert not any("private note" in h.text for h in hits)


async def test_an_empty_window_is_an_empty_result_not_an_error(omicron):
    assert await omicron.search(CH, "*", top_k=8, person="Yuan",
                                since=date(2030, 1, 1)) == []


async def test_an_unknown_person_finds_nothing_rather_than_everything(omicron):
    """Silently dropping an unmatched person filter would answer a question
    about someone else."""
    assert await omicron.search(CH, "*", top_k=8, person="Nobody") == []


async def test_no_filters_is_ordinary_ranked_retrieval(omicron):
    hits = await omicron.search(CH, "sample plates", top_k=3)
    assert 0 < len(hits) <= 3


# ------------------------------------------------------------ the tool schema


def test_the_tool_cannot_name_a_channel():
    """If the model could pass a channel, D2's isolation would become something
    it could argue its way around. Absent the parameter, it cannot."""
    props = SEARCH_TOOL["function"]["parameters"]["properties"]
    assert "channel" not in props and "channel_id" not in props
    assert set(props) == {"query", "person", "since", "until"}


# ------------------------------------------------------------- the tool loop


class ScriptedChat:
    """Plays back assistant turns; records what it was sent."""

    def __init__(self, *turns: ChatTurn):
        self.turns = list(turns)
        self.calls: list[dict] = []

    async def chat(self, messages, *, tools, tool_choice):
        self.calls.append({"messages": [dict(m) for m in messages],
                           "tools": tools, "tool_choice": tool_choice})
        return self.turns.pop(0) if self.turns else ChatTurn(content=NO_ANSWER)

    async def complete(self, system, user):  # legacy path, unused here
        raise AssertionError("tool-capable completer should not use complete()")


def search_call(**args) -> ChatTurn:
    return ChatTurn(content="", tool_calls=[
        ToolCall(id=f"call_{len(args)}", name="search_channel", arguments=json.dumps(args))
    ])


def answerer(retriever, chat, **kw) -> Answerer:
    return Answerer(retriever, chat, team_url=TEAM, top_k=8,
                    today=lambda: date(2026, 10, 13), **kw)


async def test_the_first_turn_must_search(omicron):
    chat = ScriptedChat(search_call(query="*", person="yuan", since="2026-09-13"),
                        ChatTurn(content="He is replacing a feedthrough."))
    await answerer(omicron, chat).answer(CH, "What is yuan doing in the last month?")
    assert chat.calls[0]["tool_choice"] == "required"


async def test_the_model_is_told_today_s_date(omicron):
    """Without it, one model omitted the window and another invented 2025."""
    chat = ScriptedChat(search_call(query="x"), ChatTurn(content=NO_ANSWER))
    await answerer(omicron, chat).answer(CH, "anything recent?")
    first_user = next(m for m in chat.calls[0]["messages"] if m["role"] == "user")
    assert "2026-10-13" in first_user["content"]


async def test_search_results_reach_the_model_with_permalinks(omicron):
    chat = ScriptedChat(search_call(query="*", person="yuan", since="2026-09-13"),
                        ChatTurn(content="ok"))
    await answerer(omicron, chat).answer(CH, "What is yuan doing?")
    tool_msgs = [m for m in chat.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs and "quick update on my experiment" in tool_msgs[0]["content"]
    assert f"{TEAM}/archives/{CH}/p" in tool_msgs[0]["content"]


async def test_cited_chunks_are_recorded_for_the_query_log(omicron):
    chat = ScriptedChat(search_call(query="*", person="yuan", since="2026-09-13"),
                        ChatTurn(content="He is replacing a feedthrough."))
    ans = await answerer(omicron, chat).answer(CH, "What is yuan doing?")
    assert len(ans.chunk_ids) == 3 and ans.searches == 1 and not ans.refused


async def test_the_model_may_search_again(omicron):
    chat = ScriptedChat(search_call(query="feedthrough"),
                        search_call(query="*", person="yuan", since="2026-09-13"),
                        ChatTurn(content="answer"))
    ans = await answerer(omicron, chat).answer(CH, "q")
    assert ans.searches == 2
    assert chat.calls[1]["tool_choice"] == "auto"


async def test_running_out_of_rounds_forces_an_answer(omicron):
    """A confused model must not loop. On the last allowed round it may not
    call tools at all."""
    chat = ScriptedChat(*[search_call(query=f"q{i}") for i in range(10)])
    await answerer(omicron, chat, max_tool_rounds=3).answer(CH, "q")
    assert chat.calls[-1]["tool_choice"] == "none"
    assert len(chat.calls) == 4  # three searching rounds, then a forced answer


async def test_the_time_budget_forces_an_answer(omicron):
    """The budget is wall-clock, not a round count: one slow round can use it."""
    clock = iter([0.0, 0.0, 55.0, 55.0, 55.0, 55.0])
    chat = ScriptedChat(search_call(query="a"), search_call(query="b"),
                        ChatTurn(content="answer"))
    a = answerer(omicron, chat, answer_budget=60.0, now=lambda: next(clock, 59.0))
    await a.answer(CH, "q")
    assert chat.calls[-1]["tool_choice"] == "none"


async def test_a_refusal_after_searching_is_a_refusal(omicron):
    chat = ScriptedChat(search_call(query="parental leave"), ChatTurn(content=NO_ANSWER))
    ans = await answerer(omicron, chat).answer(CH, "parental leave policy?")
    assert ans.refused


async def test_a_malformed_tool_call_is_reported_back_not_raised(omicron):
    bad = ChatTurn(content="", tool_calls=[
        ToolCall(id="c1", name="search_channel", arguments="{not json")])
    chat = ScriptedChat(bad, ChatTurn(content=NO_ANSWER))
    await answerer(omicron, chat).answer(CH, "q")
    tool_msg = next(m for m in chat.calls[1]["messages"] if m["role"] == "tool")
    assert "could not" in tool_msg["content"].lower()


async def test_a_bad_date_is_reported_back_not_raised(omicron):
    chat = ScriptedChat(search_call(query="x", since="last month"),
                        ChatTurn(content=NO_ANSWER))
    await answerer(omicron, chat).answer(CH, "q")
    tool_msg = next(m for m in chat.calls[1]["messages"] if m["role"] == "tool")
    assert "YYYY-MM-DD" in tool_msg["content"]


async def test_the_tool_only_ever_searches_the_question_s_channel(omicron, store, embedder):
    chat = ScriptedChat(search_call(query="*", person="yuan"), ChatTurn(content="ok"))
    await answerer(omicron, chat).answer(CH, "q")
    tool_msg = next(m for m in chat.calls[1]["messages"] if m["role"] == "tool")
    assert "private note" not in tool_msg["content"]


async def test_a_completer_without_chat_still_uses_the_legacy_path(omicron):
    class Legacy:
        async def complete(self, system, user):
            return NO_ANSWER

    ans = await Answerer(omicron, Legacy(), team_url=TEAM).answer(CH, "sample plates")
    assert ans.refused


async def test_the_legacy_path_is_told_the_date_too(omicron):
    """Turning tool search off must not bring the date bug back."""
    class Legacy:
        def __init__(self):
            self.user = ""

        async def complete(self, system, user):
            self.user = user
            return NO_ANSWER

    legacy = Legacy()
    await Answerer(omicron, legacy, team_url=TEAM,
                   today=lambda: date(2026, 10, 13)).answer(CH, "sample plates")
    assert "Today is 2026-10-13" in legacy.user
