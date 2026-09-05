from __future__ import annotations

from datetime import date

from slackqa.profiles import (
    INSTRUMENT,
    PERSON,
    Entry,
    Profile,
    Profiles,
    condense,
    parse,
    render,
    slug,
)

TODAY = date(2026, 8, 19)

SAMPLE = """\
# Markus

- kind: person
- slack-id: U03HPA39BDM
- aliases: Markus A
- endorsed-by: Dong Chen (2026-08-19)
- updated: 2026-08-19

## Abstract

Postdoc; builds and commissions the 4-probe. Cryogenics and UHV assembly.

## Timeline

### 2026-07

Installed the 220K shield; chased a shutter fault.

### 2026-05

Heat shields finished.

### 2024

Joined the group; worked on the Omicron prep chamber.
"""


def test_parse_round_trip():
    p = parse(SAMPLE)
    assert p.name == "Markus"
    assert p.kind == PERSON
    assert p.slack_id == "U03HPA39BDM"
    assert p.aliases == ["Markus A"]
    assert p.endorsed is True
    assert "Postdoc" in p.abstract
    assert [e.period for e in p.timeline] == ["2026-07", "2026-05", "2024"]

    again = parse(render(p))
    assert again.name == p.name
    assert [e.period for e in again.timeline] == [e.period for e in p.timeline]
    assert again.abstract == p.abstract


def test_timeline_stays_newest_first():
    p = Profile(name="X")
    p.add_entry("2024", "old")
    p.add_entry("2026-08", "new")
    p.add_entry("2025", "middle")
    assert [e.period for e in p.timeline] == ["2026-08", "2025", "2024"]


def test_adding_the_same_period_replaces_rather_than_duplicates():
    p = Profile(name="X")
    p.add_entry("2026-08", "first")
    p.add_entry("2026-08", "second")
    assert len(p.timeline) == 1
    assert p.timeline[0].text == "second"


def test_entry_granularity():
    assert Entry("2026-08", "x").monthly is True
    assert Entry("2026", "x").monthly is False
    assert Entry("2026-08", "x").year == "2026"


def test_entry_age():
    assert Entry("2026-08", "x").age_months(TODAY) == 0
    assert Entry("2026-02", "x").age_months(TODAY) == 6
    # A yearly entry is dated to the end of its year.
    assert Entry("2025", "x").age_months(TODAY) == 8


# ------------------------------------------------------- rolling resolution


def test_condense_folds_aged_months_into_their_year():
    p = Profile(name="X")
    p.add_entry("2026-08", "August work.")
    p.add_entry("2026-07", "July work.")
    p.add_entry("2026-01", "January work.")   # 7 months old
    p.add_entry("2025-11", "November work.")  # 9 months old

    touched = condense(p, today=TODAY)

    periods = [e.period for e in p.timeline]
    assert "2026-08" in periods and "2026-07" in periods  # inside the window
    assert "2026-01" not in periods and "2025-11" not in periods
    assert "2026" in periods and "2025" in periods
    assert sorted(touched) == ["2025", "2026"]


def test_condense_accumulates_into_an_existing_year():
    """The year's paragraph is the accumulation, not the newest fragment —
    folding must not overwrite what that year already said."""
    p = Profile(name="X")
    p.add_entry("2025", "Earlier in the year, commissioning.")
    p.add_entry("2025-11", "Then the shutter fault.")

    condense(p, today=TODAY)

    year = p.entry_for("2025")
    assert "commissioning" in year.text
    assert "shutter fault" in year.text


def test_condense_leaves_recent_months_alone():
    p = Profile(name="X")
    p.add_entry("2026-08", "recent")
    assert condense(p, today=TODAY) == []
    assert p.entry_for("2026-08") is not None


def test_condense_is_idempotent():
    p = Profile(name="X")
    p.add_entry("2025-03", "old work")
    condense(p, today=TODAY)
    before = p.entry_for("2025").text
    condense(p, today=TODAY)
    assert p.entry_for("2025").text == before


# --------------------------------------------------------------- lifecycle


def test_person_activity_follows_the_timeline():
    active = Profile(name="A", timeline=[Entry("2026-07", "x")])
    dormant = Profile(name="B", timeline=[Entry("2021-06", "x")])
    assert active.active(today=TODAY) is True
    assert dormant.active(today=TODAY) is False


def test_instruments_are_always_active():
    """A microscope nobody mentioned this month has not left the lab."""
    quiet = Profile(name="Beast", kind=INSTRUMENT, timeline=[Entry("2019", "built")])
    assert quiet.active(today=TODAY) is True


def test_profile_with_no_timeline_is_not_active():
    assert Profile(name="New").active(today=TODAY) is False


# ------------------------------------------------------------------- store


def test_save_and_load(tmp_path):
    store = Profiles(tmp_path)
    p = parse(SAMPLE)
    path = store.save(p)
    assert path.parent.name == "people"
    assert store.load("Markus", PERSON).abstract == p.abstract


def test_instruments_and_people_are_kept_apart(tmp_path):
    store = Profiles(tmp_path)
    store.save(Profile(name="createc", kind=INSTRUMENT, abstract="A 4 K STM."))
    store.save(Profile(name="Markus", kind=PERSON, abstract="A postdoc."))
    assert [p.name for p in store.all(INSTRUMENT)] == ["createc"]
    assert [p.name for p in store.all(PERSON)] == ["Markus"]
    assert len(store.all()) == 2


def test_slugs_are_filesystem_safe():
    assert slug("Jörn Bannies") == "j-rn-bannies"
    assert slug("Joel the Jeol") == "joel-the-jeol"
    assert slug("4-probe") == "4-probe"


def test_missing_profile_is_none(tmp_path):
    assert Profiles(tmp_path).load("Nobody", PERSON) is None


# ---------------------------------------------------------------- detection


def test_detect_matches_names_and_aliases(tmp_path):
    store = Profiles(tmp_path)
    store.save(Profile(name="Beast", kind=INSTRUMENT, aliases=["the beast"]))
    store.save(Profile(name="Markus", kind=PERSON))
    found = {p.name for p in store.detect("did Markus get the beast cold?")}
    assert found == {"Beast", "Markus"}


def test_detect_respects_word_boundaries(tmp_path):
    store = Profiles(tmp_path)
    store.save(Profile(name="Beast", kind=INSTRUMENT))
    assert store.detect("that was beastly hard") == []


def test_instrument_renders_a_systems_section(tmp_path):
    out = render(Profile(name="tesla", kind=INSTRUMENT, systems="LaSbTe, NbSe2"))
    assert "## Systems" in out and "LaSbTe" in out


def test_source_url_survives_round_trip():
    p = Profile(name="createc", kind=INSTRUMENT,
                source="https://lair.phas.ubc.ca/instruments/createc-4-k-uhv-stm-afm/")
    assert parse(render(p)).source == p.source


# ------------------------------------------------- per-entry provenance

ENTRY_SAMPLE = """\
# X

- kind: person

## Abstract

Someone.

## Timeline

### 2026-08

Recent work.

- endorsed-by: Dong Chen (2026-08-19)

### 2019

Old work, corrected by hand.

- edited-by: Jisun (2026-08-19)
"""


def test_entry_endorsement_round_trips():
    p = parse(ENTRY_SAMPLE)
    recent, old = p.entry_for("2026-08"), p.entry_for("2019")
    assert recent.endorsed_by.startswith("Dong Chen")
    assert recent.endorsed is True
    assert old.edited_by.startswith("Jisun")
    assert old.endorsed is False

    again = parse(render(p))
    assert again.entry_for("2026-08").endorsed_by == recent.endorsed_by
    assert again.entry_for("2019").edited_by == old.edited_by


def test_entry_metadata_is_not_swallowed_into_the_body():
    p = parse(ENTRY_SAMPLE)
    assert "endorsed-by" not in p.entry_for("2026-08").text
    assert p.entry_for("2026-08").text == "Recent work."


def test_reviewed_covers_endorsed_or_edited():
    assert Entry("2026", "x", endorsed_by="A").reviewed is True
    assert Entry("2026", "x", edited_by="B").reviewed is True
    assert Entry("2026", "x").reviewed is False


def test_updating_text_keeps_existing_provenance():
    p = parse(ENTRY_SAMPLE)
    p.add_entry("2026-08", "Rewritten text.")
    e = p.entry_for("2026-08")
    assert e.text == "Rewritten text."
    assert e.endorsed_by.startswith("Dong Chen")


def test_a_reviewed_entry_is_never_folded_away():
    """Condensation is safe only because nothing is lost — the source messages
    survive. A person's endorsement is not in those messages; it exists only
    here, so folding it would destroy it."""
    from datetime import date

    p = Profile(name="X")
    p.add_entry("2025-01", "Endorsed month.", endorsed_by="Dong Chen (2025-02-01)")
    p.add_entry("2025-02", "Ordinary month.")
    condense(p, today=date(2026, 8, 19))

    assert p.entry_for("2025-01") is not None, "an endorsed month must survive"
    assert p.entry_for("2025-01").endorsed_by.startswith("Dong Chen")
    assert p.entry_for("2025-02") is None      # ordinary month folded
    assert "Ordinary month." in p.entry_for("2025").text
