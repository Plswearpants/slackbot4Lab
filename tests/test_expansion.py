from __future__ import annotations

from slackqa.expansion import expand, strip_trigger, wants_expansion
from slackqa.glossary import Entry, Glossary

CH = "C0TEST"


class ScriptedCompleter:
    def __init__(self, *replies: str):
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, system: str, user: str) -> str:
        self.prompts.append(user)
        return self._replies.pop(0) if self._replies else ""


# -------------------------------------------------------------------- trigger


def test_trigger_words():
    for q in ["deep what was the gunk", "dig into the blue stuff",
              "search harder for the spectra", "Deep: what happened"]:
        assert wants_expansion(q), q


def test_ordinary_questions_do_not_trigger():
    # Opt-in is the whole point: a normal question must stay on the free,
    # deterministic path.
    for q in ["what is the status of the heat shield",
              "deeply cooled samples",       # 'deep' only as a word stem
              "how deep is the chamber"]:    # 'deep' not leading
        assert not wants_expansion(q), q


def test_strip_trigger_leaves_the_question():
    assert strip_trigger("deep: what was that gunk") == "what was that gunk"
    assert strip_trigger("dig what was that gunk") == "what was that gunk"
    assert strip_trigger("what was that gunk") == "what was that gunk"


# ------------------------------------------------------------------ expanding


def gl(tmp_path) -> Glossary:
    return Glossary(
        tmp_path / "g.md",
        [
            Entry(term="blue goo", definition="Unidentified blue deposit.",
                  aliases=["blue stuff"], channels=[CH]),
            Entry(term="XRD", definition="X-ray diffraction.", channels=[CH]),
            Entry(term="elsewhere", definition="Other channel.", channels=["COTHER"]),
        ],
    )


async def test_expansion_returns_bare_terms(tmp_path):
    c = ScriptedCompleter("blue goo XRD deposit contamination sample holder")
    terms = await expand("what was that gunk", c, glossary=gl(tmp_path), channel_id=CH)
    assert "blue" in terms and "XRD" in terms


async def test_channel_vocabulary_is_offered_to_the_rewriter(tmp_path):
    # Without the glossary the rewrite is guesswork about a lab it has never
    # seen; with it, "gunk" can land on a term the channel actually uses.
    c = ScriptedCompleter("blue goo")
    await expand("what was that gunk", c, glossary=gl(tmp_path), channel_id=CH)
    prompt = c.prompts[0]
    assert "blue goo" in prompt and "blue stuff" in prompt
    assert "elsewhere" not in prompt  # other channel's vocabulary stays out


async def test_prose_and_bullets_are_stripped(tmp_path):
    c = ScriptedCompleter(
        "Here are some search terms:\n"
        "- blue goo XRD deposit contamination residue\n"
        "Hope that helps!"
    )
    terms = await expand("q", c, glossary=gl(tmp_path), channel_id=CH)
    assert terms.startswith("blue goo XRD")
    assert "Hope" not in terms


def test_duplicate_terms_collapse():
    import asyncio

    c = ScriptedCompleter("XRD XRD xrd blue blue")
    terms = asyncio.run(expand("q", c))
    assert terms.split().count("XRD") == 1


async def test_model_failure_degrades_to_no_expansion():
    class Boom:
        async def complete(self, system, user):
            raise RuntimeError("provider down")

    # A failed expansion must leave the question answerable, not raise.
    assert await expand("q", Boom()) == ""


async def test_expansion_is_cached(store, tmp_path):
    c = ScriptedCompleter("blue goo XRD", "SHOULD NOT BE CALLED AGAIN")
    kw = {"glossary": gl(tmp_path), "channel_id": CH, "store": store}
    first = await expand("what was that gunk", c, **kw)
    second = await expand("what was that gunk", c, **kw)
    assert first == second
    assert len(c.prompts) == 1, "a repeated question must not spend a second call"


async def test_cache_survives_reload(store, tmp_path):
    c = ScriptedCompleter("blue goo XRD")
    await expand("q1", c, glossary=gl(tmp_path), channel_id=CH, store=store)
    assert await store.get_expansion("q1") == "blue goo XRD"


async def test_no_glossary_still_works():
    c = ScriptedCompleter("some terms here")
    assert await expand("q", c) == "some terms here"


# ------------------------------------------------------- end to end plumbing


async def test_deep_question_reaches_the_search_query(store, embedder, tmp_path):
    from slackqa.answerer import Answerer
    from slackqa.retrieval import Retriever
    from slackqa.store import Chunk

    chunk = Chunk(
        channel_id=CH, kind="window", anchor_ts="100.000000", start_ts=100.0,
        end_ts=110.0, participants=["U1"], msg_count=1,
        text="we ran XRD on the blue goo and could not match it",
    )
    vecs = embedder.embed_documents([chunk.text])
    await store.insert_chunks([chunk], embeddings=[vecs[0].tolist()])

    c = ScriptedCompleter("blue goo XRD deposit", "The XRD did not match.")
    a = Answerer(
        Retriever(store, embedder), c, team_url="https://x.slack.com",
        glossary=gl(tmp_path), store=store,
    )
    ans = await a.answer(CH, "deep what was that gunk")
    assert ans.deep is True
    assert len(c.prompts) == 2  # one expansion, one answer


async def test_ordinary_question_spends_no_expansion_call(store, embedder, tmp_path):
    from slackqa.answerer import Answerer
    from slackqa.retrieval import Retriever
    from slackqa.store import Chunk

    chunk = Chunk(
        channel_id=CH, kind="window", anchor_ts="100.000000", start_ts=100.0,
        end_ts=110.0, participants=["U1"], msg_count=1, text="the heat shield is installed",
    )
    vecs = embedder.embed_documents([chunk.text])
    await store.insert_chunks([chunk], embeddings=[vecs[0].tolist()])

    c = ScriptedCompleter("An answer.")
    a = Answerer(
        Retriever(store, embedder), c, team_url="https://x.slack.com",
        glossary=gl(tmp_path), store=store,
    )
    ans = await a.answer(CH, "what is the status of the heat shield")
    assert ans.deep is False
    assert len(c.prompts) == 1  # answering only
