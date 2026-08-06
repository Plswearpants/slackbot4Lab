from __future__ import annotations

import pytest

from slackqa.answerer import (
    NO_ANSWER,
    Answerer,
    build_user_prompt,
    permalink,
)
from slackqa.retrieval import Retriever
from slackqa.store import Chunk

CH = "C0TEST"
TEAM = "https://acme.slack.com"


class ScriptedCompleter:
    """Returns queued replies in order; records the prompts it received."""

    def __init__(self, *replies: str):
        self._replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self._replies.pop(0) if self._replies else "fallback"


def mk(text: str, start: float, kind: str = "window") -> Chunk:
    return Chunk(
        channel_id=CH,
        kind=kind,
        anchor_ts=f"{start:.6f}",
        start_ts=start,
        end_ts=start + 10,
        participants=["U1"],
        msg_count=2,
        text=text,
    )


async def seed(store, embedder, chunks):
    vecs = embedder.embed_documents([c.text for c in chunks])
    await store.insert_chunks(chunks, embeddings=[v.tolist() for v in vecs])


def make(store, embedder, completer):
    return Answerer(Retriever(store, embedder), completer, team_url=TEAM, top_k=5)


# ------------------------------------------------------------------ permalink


def test_permalink_strips_dot():
    assert permalink(TEAM, CH, "1700000000.123456") == (
        f"{TEAM}/archives/{CH}/p1700000000123456"
    )


def test_permalink_tolerates_trailing_slash():
    assert permalink(TEAM + "/", CH, "1.2").startswith(f"{TEAM}/archives/")


# --------------------------------------------------------------- prompt shape


def test_prompt_includes_permalink_and_text():
    from slackqa.retrieval import Hit

    chunk = mk("we chose postgres", 1700000000)
    hit = Hit(chunk_id=1, score=0.5, chunk={**chunk.__dict__, "id": 1})
    prompt = build_user_prompt("what db?", [hit], TEAM, CH)
    assert "what db?" in prompt
    assert "we chose postgres" in prompt
    assert f"{TEAM}/archives/{CH}/p1700000000000000" in prompt
    assert "2023-11-14" in prompt


# ------------------------------------------------------------------ answering


async def test_answers_from_retrieved_chunks(store, embedder):
    await seed(store, embedder, [mk("we migrated to postgres", 100)])
    c = ScriptedCompleter("We use postgres. <link|2023>")
    ans = await make(store, embedder, c).answer(CH, "what database?")
    assert ans.refused is False
    assert "postgres" in ans.text
    assert ans.chunk_ids


async def test_refuses_without_calling_model_when_index_empty(store, embedder):
    c = ScriptedCompleter("should never be used")
    ans = await make(store, embedder, c).answer(CH, "anything at all")
    assert ans.refused is True
    assert c.calls == []  # no API call spent on an empty index


async def test_no_answer_sentinel_becomes_refusal(store, embedder):
    await seed(store, embedder, [mk("unrelated budget chatter", 100)])
    c = ScriptedCompleter(NO_ANSWER)
    ans = await make(store, embedder, c).answer(CH, "what database?")
    assert ans.refused is True
    assert NO_ANSWER not in ans.text
    assert "couldn't find" in ans.text


async def test_empty_reply_becomes_refusal(store, embedder):
    await seed(store, embedder, [mk("postgres notes", 100)])
    ans = await make(store, embedder, ScriptedCompleter("   ")).answer(CH, "db?")
    assert ans.refused is True


async def test_refinement_round_triggers_second_retrieval(store, embedder):
    await seed(store, embedder, [mk("we ship to prod on fridays", 100)])
    c = ScriptedCompleter("SEARCH: deploy schedule", "We deploy on Fridays.")
    ans = await make(store, embedder, c).answer(CH, "when do we release?")
    assert ans.searches == 2
    assert len(c.calls) == 2
    assert "Fridays" in ans.text


async def test_refinement_is_capped_at_one(store, embedder):
    await seed(store, embedder, [mk("deploy notes", 100)])
    # Model tries to search twice; the second attempt must not spawn a third call.
    c = ScriptedCompleter("SEARCH: first", "SEARCH: second")
    ans = await make(store, embedder, c).answer(CH, "q")
    assert len(c.calls) == 2
    assert ans.searches == 2
    assert "SEARCH:" not in ans.text


async def test_stray_search_line_stripped_from_final_answer(store, embedder):
    await seed(store, embedder, [mk("deploy notes", 100)])
    c = ScriptedCompleter("SEARCH: x", "The answer.\nSEARCH: ignored")
    ans = await make(store, embedder, c).answer(CH, "q")
    assert "SEARCH:" not in ans.text
    assert "The answer." in ans.text


async def test_refinement_keeps_original_hits_when_refined_search_finds_nothing(
    store, embedder
):
    await seed(store, embedder, [mk("postgres migration notes", 100)])
    c = ScriptedCompleter("SEARCH: !!!", "Answer from original excerpts.")
    ans = await make(store, embedder, c).answer(CH, "postgres?")
    assert ans.chunk_ids  # did not end up with an empty evidence set


async def test_system_prompt_demands_grounding_and_refusal():
    from slackqa.answerer import SYSTEM_PROMPT

    assert NO_ANSWER in SYSTEM_PROMPT
    assert "PERMALINK" in SYSTEM_PROMPT
    assert "outside knowledge" in SYSTEM_PROMPT


async def test_answer_is_channel_scoped(store, embedder):
    other = Chunk(
        channel_id="C0OTHER",
        kind="window",
        anchor_ts="100.000000",
        start_ts=100.0,
        end_ts=110.0,
        participants=["U9"],
        msg_count=1,
        text="postgres in the other channel",
    )
    await seed(store, embedder, [other])
    c = ScriptedCompleter("should never be used")
    ans = await make(store, embedder, c).answer(CH, "postgres?")
    assert ans.refused is True
    assert c.calls == []


# ------------------------------------------------------- openrouter completer


class _FakeCompletions:
    def __init__(self, resp):
        self._resp = resp
        self.kwargs: dict = {}

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self._resp


class _Resp:
    def __init__(self, content):
        if content is None:
            self.choices = []
        else:
            msg = type("M", (), {"content": content})()
            self.choices = [type("C", (), {"message": msg})()]


def _completer_with(resp):
    from slackqa.answerer import OpenRouterCompleter

    c = OpenRouterCompleter("sk-or-test", "anthropic/claude-sonnet-5")
    fake = _FakeCompletions(resp)
    c._client = type("Cl", (), {"chat": type("Ch", (), {"completions": fake})()})()
    return c, fake


async def test_openrouter_sends_system_as_first_message():
    # OpenRouter takes the system prompt in the messages array, not as a
    # separate parameter — the one shape difference from Anthropic's own API.
    c, fake = _completer_with(_Resp("the answer"))
    assert await c.complete("SYS", "USER") == "the answer"
    msgs = fake.kwargs["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1] == {"role": "user", "content": "USER"}
    assert fake.kwargs["model"] == "anthropic/claude-sonnet-5"


async def test_openrouter_empty_choices_returns_empty_not_indexerror():
    # OpenRouter can return 200 with no choices when an upstream provider
    # fails; that must degrade to a refusal, not a stack trace in Slack.
    c, _ = _completer_with(_Resp(None))
    assert await c.complete("s", "u") == ""


async def test_openrouter_null_content_returns_empty():
    c, _ = _completer_with(_Resp(None if False else ""))
    assert await c.complete("s", "u") == ""


async def test_empty_completion_becomes_refusal_end_to_end(store, embedder):
    await seed(store, embedder, [mk("postgres notes", 100)])
    ans = await make(store, embedder, ScriptedCompleter("")).answer(CH, "db?")
    assert ans.refused is True


# ------------------------------------------------------- credential checking


async def test_check_credentials_raises_on_401(monkeypatch):
    import httpx

    from slackqa.answerer import CredentialsError, OpenRouterCompleter

    class FakeAsyncClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            return httpx.Response(401, text='{"error":{"message":"User not found."}}')

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    c = OpenRouterCompleter("sk-or-dead", "anthropic/claude-sonnet-5")
    with pytest.raises(CredentialsError, match="openrouter.ai/keys"):
        await c.check_credentials()


async def test_check_credentials_passes_on_200(monkeypatch):
    import httpx

    from slackqa.answerer import OpenRouterCompleter

    class FakeAsyncClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            assert headers["Authorization"] == "Bearer sk-or-live"
            assert url.endswith("/key")
            return httpx.Response(200, text='{"data":{"label":"test"}}')

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    await OpenRouterCompleter("sk-or-live", "m").check_credentials()  # must not raise


# ------------------------------------------------------------- thread memory


from slackqa.answerer import Turn, needs_thread_context, render_thread  # noqa: E402


def test_needs_thread_context_for_anaphoric_followup():
    # The real case that got refused in production.
    assert needs_thread_context("no, it does not look right, because Markus is already there")
    assert needs_thread_context("why not?")
    assert needs_thread_context("what about that one")


def test_self_contained_question_does_not_pull_thread_context():
    assert not needs_thread_context(
        "what is the status of the electronic breakout box project"
    )


def test_render_thread_labels_bot_as_you():
    out = render_thread([Turn("alice", "hi"), Turn("bot", "hello", is_bot=True)])
    assert "alice: hi" in out and "you: hello" in out


def test_render_thread_keeps_most_recent_when_trimming():
    turns = [Turn("u", f"message number {i} " + "x" * 100) for i in range(20)]
    out = render_thread(turns, max_chars=300)
    assert "message number 19" in out
    assert "message number 0 " not in out


async def test_thread_turns_reach_the_prompt(store, embedder):
    await seed(store, embedder, [mk("we chose postgres", 100)])
    c = ScriptedCompleter("ok")
    thread = [Turn("alice", "what database?"), Turn("bot", "mysql", is_bot=True)]
    await make(store, embedder, c).answer(CH, "no that's wrong", thread=thread)
    prompt = c.calls[0][1]
    assert "Conversation so far" in prompt
    assert "alice: what database?" in prompt
    assert "you: mysql" in prompt


async def test_no_thread_section_when_not_in_a_thread(store, embedder):
    await seed(store, embedder, [mk("postgres", 100)])
    c = ScriptedCompleter("ok")
    await make(store, embedder, c).answer(CH, "what database do we use here")
    assert "Conversation so far" not in c.calls[0][1]


async def test_system_prompt_covers_thread_and_glossary():
    from slackqa.answerer import SYSTEM_PROMPT

    assert "Conversation so far" in SYSTEM_PROMPT
    assert "accept the correction" in SYSTEM_PROMPT
    assert "UNENDORSED" in SYSTEM_PROMPT


# ----------------------------------------------------------------- glossary


def _glossary(tmp_path, text):
    from slackqa.glossary import Glossary

    p = tmp_path / "g.md"
    p.write_text(text)
    return Glossary.load(p)


async def test_glossary_definition_injected_when_term_present(store, embedder, tmp_path):
    from slackqa.answerer import Answerer
    from slackqa.retrieval import Retriever

    g = _glossary(
        tmp_path,
        "## beast\n\nThe older breakout box.\n\n- endorsed-by: Sarah (2026-01-01)\n",
    )
    await seed(store, embedder, [mk("deploy notes", 100)])
    c = ScriptedCompleter("ok")
    a = Answerer(Retriever(store, embedder), c, team_url=TEAM, top_k=5, glossary=g)
    await a.answer(CH, "is the beast still in use?")
    prompt = c.calls[0][1]
    assert "The older breakout box" in prompt
    assert "endorsed by Sarah" in prompt


async def test_unendorsed_definition_is_flagged(store, embedder, tmp_path):
    from slackqa.answerer import Answerer
    from slackqa.retrieval import Retriever

    g = _glossary(tmp_path, "## beast\n\nThe older breakout box.\n")
    await seed(store, embedder, [mk("deploy notes", 100)])
    c = ScriptedCompleter("ok")
    a = Answerer(Retriever(store, embedder), c, team_url=TEAM, top_k=5, glossary=g)
    await a.answer(CH, "is the beast still in use?")
    assert "UNENDORSED" in c.calls[0][1]


async def test_no_glossary_block_when_no_term_matches(store, embedder, tmp_path):
    from slackqa.answerer import Answerer
    from slackqa.retrieval import Retriever

    g = _glossary(tmp_path, "## beast\n\nThe older breakout box.\n")
    await seed(store, embedder, [mk("deploy notes", 100)])
    c = ScriptedCompleter("ok")
    a = Answerer(Retriever(store, embedder), c, team_url=TEAM, top_k=5, glossary=g)
    await a.answer(CH, "how do we deploy the service")
    assert "Glossary definitions" not in c.calls[0][1]


async def test_answerer_works_without_a_glossary(store, embedder):
    await seed(store, embedder, [mk("postgres", 100)])
    ans = await make(store, embedder, ScriptedCompleter("fine")).answer(CH, "db?")
    assert ans.refused is False
