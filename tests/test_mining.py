from __future__ import annotations

from datetime import date, timedelta

from slackqa.glossary import Entry, Glossary, SkipList
from slackqa.mining import (
    find_candidates,
    mine,
    parse_fields,
    parse_triage,
    refresh_volatile,
)
from slackqa.store import Chunk

CH = "C0TEST"


class ScriptedCompleter:
    def __init__(self, *replies: str):
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, system: str, user: str) -> str:
        self.prompts.append(user)
        return self._replies.pop(0) if self._replies else "UNCLEAR"


# ------------------------------------------------------------ candidate terms


def test_finds_multiword_phrases():
    # The whole point of the rework: "breakout box" is lowercase and two words,
    # which an acronym regex cannot see.
    texts = {i: f"we wired the breakout box today {i}" for i in range(3)}
    assert "breakout box" in {c.term for c in find_candidates(texts, min_chunks=3)}


def test_finds_acronyms_too():
    texts = {i: "top up the LN2 dewar" for i in range(3)}
    assert "LN2" in {c.term for c in find_candidates(texts, min_chunks=3)}


def test_phrase_must_span_separate_conversations():
    assert find_candidates({1: "breakout box " * 20}, min_chunks=3) == []


def test_phrases_do_not_start_or_end_on_glue_words():
    texts = {i: "the breakout box is" for i in range(3)}
    terms = {c.term for c in find_candidates(texts, min_chunks=3)}
    assert "the breakout" not in terms
    assert "box is" not in terms
    assert "breakout box" in terms


def test_chatter_phrases_filtered():
    texts = {i: "the meeting today was about the thread link" for i in range(4)}
    terms = {c.term for c in find_candidates(texts, min_chunks=3)}
    assert not any("meeting" in t or "thread" in t for t in terms)


def test_ranks_by_breadth():
    texts = {
        1: "sample holder and load lock",
        2: "sample holder and load lock",
        3: "sample holder",
        4: "sample holder",
    }
    assert find_candidates(texts, min_chunks=2)[0].term == "sample holder"


# -------------------------------------------------------------- reply parsing


def test_parse_triage():
    reply = "breakout box :: instrument\ncharge order :: phenomenon\nCAD :: reject"
    assert parse_triage(reply) == {
        "breakout box": "instrument",
        "charge order": "phenomenon",
        "CAD": "reject",
    }


def test_parse_triage_ignores_junk_lines():
    assert parse_triage("here you go:\nfoo :: instrument\nnonsense") == {
        "foo": "instrument"
    }


def test_parse_triage_rejects_unknown_kind():
    assert parse_triage("foo :: vegetable") == {}


def test_parse_fields():
    reply = (
        "DEFINITION: A 12x6x4 box adapting two D25 connectors to 36 BNC cables.\n"
        "STATUS: on build at the electronic shop\n"
        "TIMELINE: expected complete by 2026-08-13\n"
        "ALIASES: breakout-box, BOB\n"
    )
    f = parse_fields(reply)
    assert "D25" in f["definition"]
    assert f["status"] == "on build at the electronic shop"
    assert f["timeline"] == "expected complete by 2026-08-13"
    assert f["aliases"] == "breakout-box, BOB"


def test_parse_fields_omits_absent():
    assert set(parse_fields("DEFINITION: just a definition")) == {"definition"}


def test_parse_fields_joins_wrapped_lines():
    f = parse_fields("DEFINITION: first line\n  continued here\nSTATUS: ok")
    assert f["definition"] == "first line continued here"


# -------------------------------------------------------------------- mining


async def seed_chunks(store, embedder, texts):
    chunks = [
        Chunk(
            channel_id=CH,
            kind="window",
            anchor_ts=f"{100 + i}.000000",
            start_ts=100.0 + i,
            end_ts=110.0 + i,
            participants=["U1"],
            msg_count=2,
            text=t,
        )
        for i, t in enumerate(texts)
    ]
    vecs = embedder.embed_documents([c.text for c in chunks])
    await store.insert_chunks(chunks, embeddings=[v.tolist() for v in vecs])


BOX_TEXTS = [
    "the breakout box design is finalized",
    "breakout box will use D25 connectors",
    "breakout box goes to the shop tomorrow",
]


async def test_mine_creates_rich_entry(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(tmp_path / "g.md")
    c = ScriptedCompleter(
        "breakout box :: instrument",
        "DEFINITION: A 12x6x4 box adapting two D25 connectors to 36 BNC cables.\n"
        "STATUS: on build\nTIMELINE: complete by 2026-08-13\nALIASES: BOB\n",
    )

    added = await mine(store, g, c, CH, min_chunks=3)

    assert added == ["breakout box"]
    e = g.get("breakout box")
    assert e.kind == "instrument"
    assert "D25" in e.definition
    assert e.status == "on build"
    assert e.timeline == "complete by 2026-08-13"
    assert e.as_of == date.today().isoformat()
    assert e.aliases == ["BOB"]
    assert e.endorsed is False


async def test_rejected_terms_go_to_skip_list(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(tmp_path / "g.md")
    skip = SkipList(tmp_path / "skip.txt")
    await mine(store, g, ScriptedCompleter("breakout box :: reject"), CH,
               skip=skip, min_chunks=3)
    assert "breakout box" in skip
    assert SkipList.load(tmp_path / "skip.txt").__contains__("breakout box")


async def test_skipped_terms_are_never_retriaged(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(tmp_path / "g.md")
    skip = SkipList(tmp_path / "skip.txt")
    skip.add("breakout box")
    c = ScriptedCompleter("should not be called")
    await mine(store, g, c, CH, skip=skip, min_chunks=3)
    assert c.prompts == []  # not a single model call spent


async def test_unjudged_candidates_are_also_skipped(store, embedder, tmp_path):
    # A term the model silently drops must not be re-paid-for every pass.
    await seed_chunks(store, embedder, BOX_TEXTS)
    skip = SkipList(tmp_path / "skip.txt")
    await mine(store, Glossary(tmp_path / "g.md"), ScriptedCompleter(""), CH,
               skip=skip, min_chunks=3)
    assert "breakout box" in skip


async def test_unclear_definition_is_skipped(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(tmp_path / "g.md")
    skip = SkipList(tmp_path / "skip.txt")
    c = ScriptedCompleter("breakout box :: instrument", "UNCLEAR")
    assert await mine(store, g, c, CH, skip=skip, min_chunks=3) == []
    assert "breakout box" in skip


async def test_mine_skips_already_defined(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(tmp_path / "g.md", [Entry(term="breakout box", definition="known")])
    c = ScriptedCompleter()
    assert await mine(store, g, c, CH, min_chunks=3) == []
    assert c.prompts == []


async def test_triage_failure_is_survivable(store, embedder, tmp_path):
    class Boom:
        async def complete(self, system, user):
            raise RuntimeError("provider down")

    await seed_chunks(store, embedder, BOX_TEXTS)
    assert await mine(store, Glossary(tmp_path / "g.md"), Boom(), CH, min_chunks=3) == []


async def test_no_status_means_no_as_of_stamp(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(tmp_path / "g.md")
    c = ScriptedCompleter("breakout box :: instrument", "DEFINITION: A box.")
    await mine(store, g, c, CH, min_chunks=3)
    assert g.get("breakout box").as_of is None


# ------------------------------------------------------------------- refresh


def _entry(days_old: int, **kw) -> Entry:
    return Entry(
        term="breakout box",
        definition="A box.",
        status="on build",
        as_of=(date.today() - timedelta(days=days_old)).isoformat(),
        **kw,
    )


def test_stale_detection():
    assert _entry(10).stale(7) is True
    assert _entry(2).stale(7) is False
    assert Entry(term="x", definition="d").stale(7) is False  # no volatile fields
    assert Entry(term="x", status="s").stale(7) is True  # volatile, never stamped


async def test_refresh_updates_status_and_stamp(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(tmp_path / "g.md", [_entry(30)])
    c = ScriptedCompleter("STATUS: delivered and installed\nTIMELINE: done 2026-08-12")

    updated = await refresh_volatile(store, g, c, CH, max_age_days=7)

    assert updated == ["breakout box"]
    e = g.get("breakout box")
    assert e.status == "delivered and installed"
    assert e.as_of == date.today().isoformat()


async def test_refresh_leaves_fresh_entries_alone(store, embedder, tmp_path):
    g = Glossary(tmp_path / "g.md", [_entry(1)])
    c = ScriptedCompleter("STATUS: should not be used")
    assert await refresh_volatile(store, g, c, CH, max_age_days=7) == []
    assert c.prompts == []


async def test_refresh_never_touches_endorsed_entries(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(tmp_path / "g.md", [_entry(99, endorsed_by="Dong Chen (2026-01-01)")])
    c = ScriptedCompleter("STATUS: should not be used")
    assert await refresh_volatile(store, g, c, CH, max_age_days=7) == []
    assert g.get("breakout box").status == "on build"


async def test_nochange_restamps_without_editing(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(tmp_path / "g.md", [_entry(30)])
    await refresh_volatile(store, g, ScriptedCompleter("NOCHANGE"), CH, max_age_days=7)
    e = g.get("breakout box")
    assert e.status == "on build"
    assert e.as_of == date.today().isoformat()  # re-checked, so not stale again


# ------------------------------------------------------------ alias handling


def test_clean_aliases_survives_commas_inside_parens():
    from slackqa.mining import clean_aliases

    # The real failure: naive comma-splitting produced "4-probe JT (cry".
    assert clean_aliases("4-probe JT (cryostat, JT stage), 4P system") == [
        "4-probe JT",
        "4P system",
    ]


def test_clean_aliases_drops_fragments_and_junk():
    from slackqa.mining import clean_aliases

    assert clean_aliases("") == []
    assert clean_aliases("the, a, ") == []
    assert clean_aliases("x" * 60) == []


def test_clean_aliases_dedupes_case_insensitively():
    from slackqa.mining import clean_aliases

    assert clean_aliases("BOB, bob, Bob") == ["BOB"]


async def test_term_already_an_alias_is_not_double_added(store, embedder, tmp_path):
    # "stm head" arrived as its own candidate after STM claimed it as an alias.
    # It must not be reported as added, and must not cost a definition call.
    await seed_chunks(store, embedder, [
        "the stm head and stm chamber", "stm head wiring", "stm head again",
    ])
    g = Glossary(tmp_path / "g.md", [
        Entry(term="STM", definition="d", aliases=["stm head"])
    ])
    c = ScriptedCompleter("stm head :: instrument", "DEFINITION: should not happen")
    added = await mine(store, g, c, CH, min_chunks=3)
    assert "stm head" not in added
    assert len([e for e in g.entries if e.term.lower() == "stm head"]) == 0


async def test_added_list_reflects_what_was_actually_stored(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(tmp_path / "g.md")
    c = ScriptedCompleter(
        "breakout box :: instrument",
        "DEFINITION: A box.\nALIASES: BOB\n",
    )
    added = await mine(store, g, c, CH, min_chunks=3)
    assert added == [e.term for e in g.entries]


def test_parse_fields_bare_label_does_not_pollute_previous_field():
    # Observed in production: the model wrote "TIMELINE" with no colon to mean
    # "none", and it was appended to STATUS.
    f = parse_fields("STATUS: awaiting vendor input\nTIMELINE")
    assert f["status"] == "awaiting vendor input"
    assert "timeline" not in f


def test_parse_fields_bare_label_starts_a_new_field():
    f = parse_fields("STATUS\nsomething")
    assert f["status"] == "something"


def test_clean_aliases_drops_self_reference():
    from slackqa.mining import clean_aliases

    # "main chamber (aka main chamber)" was appearing in the real glossary.
    assert clean_aliases("main chamber", term="main chamber") == []
    assert clean_aliases("main chambers", term="main chamber") == []


def test_clean_aliases_drops_none_placeholders():
    from slackqa.mining import clean_aliases

    # The model writes "none confirmed" instead of omitting the field.
    assert clean_aliases("none confirmed") == []
    assert clean_aliases("N/A, unknown") == []


def test_clean_aliases_rejects_prose_non_answers():
    from slackqa.mining import clean_aliases

    # Real output: 'none noted beyond "ion pump'
    assert clean_aliases('none noted beyond "ion pump') == []
    assert clean_aliases("no other names used") == []


# ------------------------------------------------------------ channel scoping


async def test_mined_entries_are_scoped_to_their_channel(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(tmp_path / "g.md")
    c = ScriptedCompleter("breakout box :: instrument", "DEFINITION: A box.")
    await mine(store, g, c, CH, min_chunks=3)
    assert g.get("breakout box", CH).channels == [CH]


async def test_same_term_can_be_mined_separately_per_channel(store, embedder, tmp_path):
    # "breakout box" names different hardware in #4probe and #createc; mining
    # the second channel must not be blocked by the first channel's entry.
    await seed_chunks(store, embedder, BOX_TEXTS)
    g = Glossary(
        tmp_path / "g.md",
        [Entry(term="breakout box", definition="other channel's box",
               channels=["COTHER"])],
    )
    c = ScriptedCompleter("breakout box :: instrument", "DEFINITION: our box.")
    added = await mine(store, g, c, CH, min_chunks=3)
    assert added == ["breakout box"]
    assert g.get("breakout box", CH).definition == "our box."
    assert g.get("breakout box", "COTHER").definition == "other channel's box"


async def test_refresh_only_touches_this_channels_entries(store, embedder, tmp_path):
    await seed_chunks(store, embedder, BOX_TEXTS)
    other = _entry(30)
    other.channels = ["COTHER"]
    g = Glossary(tmp_path / "g.md", [other])
    c = ScriptedCompleter("STATUS: should not be used")
    assert await refresh_volatile(store, g, c, CH, max_age_days=7) == []
    assert c.prompts == []
