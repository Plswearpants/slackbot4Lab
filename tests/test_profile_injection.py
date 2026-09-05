"""Which profiles reach the prompt, and how ambiguity is handled."""
from __future__ import annotations

import pytest

from slackqa.answerer import profile_block
from slackqa.profiles import PERSON, Profile, Profiles


@pytest.fixture
def store(tmp_path):
    s = Profiles(tmp_path)
    for name, abstract in [
        ("Will Ho", "Builds cryostat electronics."),
        ("Alex Tubby", "Runs the Createc."),
        ("Alexander Reed", "Works on ARPES at Tesla."),
        ("Alex Fournier", "Cryostat maintenance."),
        ("Jisun", "Molecular assembly on Ag(111)."),
    ]:
        p = Profile(name=name, kind=PERSON, abstract=abstract)
        p.add_entry("2026-08", f"{name} did things.")
        s.save(p)
    return s


def names(profiles):
    return sorted(p.name for p in profiles)


# ------------------------------------------------------- unambiguous matching


def test_a_full_name_is_certain(store):
    certain, ambiguous = store.candidates("what is Jisun working on?")
    assert names(certain) == ["Jisun"] and ambiguous == []


def test_a_unique_first_name_is_certain(store):
    certain, _ = store.candidates("what is Jisun up to")
    assert "Jisun" in names(certain)


def test_nobody_named_means_nothing_injected(store):
    certain, ambiguous = store.candidates("why did the tip crash last night")
    assert certain == [] and ambiguous == []


# ------------------------------------- a first name that is an ordinary word


def test_will_the_verb_does_not_summon_will_the_person(store):
    """The single highest-frequency false positive available to us: 'will'
    opens a large share of the questions this bot is asked."""
    for q in ["will the tip crash tonight?",
              "Will the Createc be free tomorrow?",
              "when will the beast warm up"]:
        certain, ambiguous = store.candidates(q)
        assert names(certain) == [] and ambiguous == [], q


def test_will_ho_by_full_name_still_works(store):
    certain, _ = store.candidates("ask Will Ho about the breakout box")
    assert names(certain) == ["Will Ho"]


def test_a_lowercase_first_name_is_not_a_name(store):
    # "alex fixed it" is likelier a typo than a reference; require the capital.
    certain, ambiguous = store.candidates("alex said it was fine")
    assert certain == [] and ambiguous == []


# ------------------------------------------------------------ ambiguity


def test_two_people_share_a_first_name(store):
    certain, ambiguous = store.candidates("what is Alex working on?")
    assert certain == []
    assert len(ambiguous) == 1
    assert names(ambiguous[0]) == ["Alex Fournier", "Alex Tubby"]


def test_the_excerpts_resolve_the_ambiguity_without_the_model(store):
    """If only one Alex appears in what was retrieved, that is the Alex meant —
    no candidate list, no decision handed to the model."""
    certain, ambiguous = store.candidates(
        "what is Alex working on?",
        evidence="Alex Tubby reported the Createc tip was blunt again.",
    )
    assert names(certain) == ["Alex Tubby"] and ambiguous == []


def test_the_roster_resolves_it_too(store):
    certain, ambiguous = store.candidates(
        "what is Alex working on?", roster=["Alex Fournier", "Jisun"]
    )
    assert names(certain) == ["Alex Fournier"] and ambiguous == []


def test_evidence_naming_both_stays_ambiguous(store):
    _, ambiguous = store.candidates(
        "what is Alex working on?",
        evidence="Alex Tubby and Alex Fournier disagreed about the setpoint.",
    )
    assert len(ambiguous) == 1 and len(ambiguous[0]) == 2


# ------------------------------------------------------------ rendering


def test_the_block_marks_profiles_as_uncitable(store):
    text = profile_block(store.candidates("what is Jisun doing")[0])
    assert "never be cited" in text
    assert "Molecular assembly" in text


def test_candidates_are_labelled_as_mutually_exclusive(store):
    """Unlabelled, three profiles for one name read as three relevant people
    and get blended into a composite who does not exist."""
    certain, ambiguous = store.candidates("what is Alex working on?")
    text = profile_block(certain, ambiguous)
    assert "CANDIDATES" in text
    assert "At most one is meant" in text
    assert "Alex Tubby" in text and "Alex Fournier" in text


def test_candidates_are_abstracts_only(store):
    """Full timelines for people who are probably not meant is exactly the
    context the retrieved excerpts need."""
    _, ambiguous = store.candidates("what is Alex working on?")
    text = profile_block((), ambiguous)
    assert "did things" not in text


def test_a_certain_profile_gets_its_timeline(store):
    text = profile_block(store.candidates("what is Jisun doing")[0])
    assert "2026-08" in text


def test_nothing_named_renders_nothing():
    assert profile_block([], []) == ""


# ------------------------------------------- nicknames are declared, not guessed


def test_a_declared_alias_creates_real_ambiguity(store, tmp_path):
    """Alex-for-Alexander is not inferred — guessing that Jo means John or Joan
    is exactly the sort of confident wrong answer this bot exists to avoid. A
    person declares it, and then it counts."""
    p = store.load("Alexander Reed", PERSON)
    p.aliases = ["Alex"]
    store.save(p)

    _, ambiguous = store.candidates("what is Alex working on?")
    assert "Alexander Reed" in names(ambiguous[0])


def test_a_full_name_is_not_re_read_as_a_bare_first_name(store):
    """"Alex Tubby fixed it" contains "Alex" followed by a word boundary. Left
    alone, the full name would be downgraded back into an ambiguous guess."""
    certain, ambiguous = store.candidates("Alex Tubby fixed the tip")
    assert names(certain) == ["Alex Tubby"] and ambiguous == []
