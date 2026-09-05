from __future__ import annotations

from datetime import UTC, date, datetime

from slackqa.profiles import INSTRUMENT, PERSON, Profile, Profiles
from slackqa.profiling import (
    build_profile,
    group_chunks,
    mine_materials,
    periods_to_build,
)

TODAY = date(2026, 8, 19)


class ScriptedCompleter:
    def __init__(self, *replies: str):
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, system: str, user: str) -> str:
        self.prompts.append(user)
        return self._replies.pop(0) if self._replies else "A generated summary."


def chunk(when: str, text: str) -> dict:
    ts = datetime.fromisoformat(when).replace(tzinfo=UTC).timestamp()
    return {"text": text, "start_ts": ts, "id": 1}


# ------------------------------------------------------------ material mining


def test_mines_real_formulae():
    texts = ["we cleaved PtSn4 today"] * 4 + ["ZrSiTe looks clean"] * 3
    got = {m.formula for m in mine_materials(texts)}
    assert {"PtSn4", "ZrSiTe"} <= got


def test_slack_ids_are_not_mistaken_for_compounds():
    """U8JQ4HFV3 parses as element symbols; only an explicit exclusion keeps
    the lab's colleagues out of its list of sample systems."""
    texts = ["<@U8JQ4HFV3> look at this"] * 10
    assert mine_materials(texts) == []


def test_techniques_and_hardware_are_excluded():
    texts = ["ran LEED and XPS, checked the RGA and the DN40 flange"] * 10
    assert mine_materials(texts) == []


def test_rare_mentions_are_dropped():
    assert mine_materials(["one mention of NbSe2"], floor=3) == []


def test_ordered_by_how_much_they_are_discussed():
    texts = ["NbSe2"] * 10 + ["LaSbTe"] * 30
    assert [m.formula for m in mine_materials(texts)][0] == "LaSbTe"


# ------------------------------------------------------------------ bucketing


def test_recent_months_bucket_monthly_and_older_yearly():
    chunks = [
        chunk("2026-08-01", "this month"),
        chunk("2026-07-01", "last month"),
        chunk("2026-01-01", "seven months ago"),
        chunk("2019-05-01", "years ago"),
    ]
    buckets = group_chunks(chunks, today=TODAY, months=6)
    assert "2026-08" in buckets and "2026-07" in buckets
    assert "2026" in buckets and "2019" in buckets
    assert "2026-01" not in buckets


def test_periods_already_written_are_not_rebuilt():
    existing = Profile(name="X")
    existing.add_entry("2026-08", "already done")
    todo = periods_to_build({"2026-08": [], "2026-07": []}, existing, TODAY)
    assert [p for p, _ in todo] == ["2026-07"]


def test_everything_is_todo_for_a_new_profile():
    todo = periods_to_build({"2025": [], "2026-08": []}, None, TODAY)
    assert {p for p, _ in todo} == {"2025", "2026-08"}
    assert dict(todo)["2026-08"] is True   # monthly
    assert dict(todo)["2025"] is False     # yearly


# -------------------------------------------------------------- build a person


async def test_builds_a_person_profile(tmp_path):
    profiles = Profiles(tmp_path)
    c = ScriptedCompleter("Installed the heat shields.", "Markus builds the 4-probe.")
    p = await build_profile(
        None, c, profiles, name="Markus", kind=PERSON, slack_id="U1",
        chunks=[chunk("2026-08-01", "Markus installed the 220K shield")],
        today=TODAY,
    )
    assert p.entry_for("2026-08").text == "Installed the heat shields."
    assert "builds the 4-probe" in p.abstract
    assert p.updated == "2026-08-19"
    assert profiles.load("Markus", PERSON) is not None


async def test_rebuild_only_generates_missing_periods(tmp_path):
    profiles = Profiles(tmp_path)
    chunks = [chunk("2026-08-01", "August"), chunk("2026-07-01", "July")]
    first = ScriptedCompleter("Aug entry.", "Jul entry.", "An abstract.")
    await build_profile(None, first, profiles, name="X", kind=PERSON,
                        chunks=chunks, today=TODAY)
    calls_first = len(first.prompts)

    second = ScriptedCompleter("SHOULD NOT BE USED")
    await build_profile(None, second, profiles, name="X", kind=PERSON,
                        chunks=chunks, today=TODAY)
    assert calls_first >= 2
    assert second.prompts == [], "an unchanged period must not be regenerated"


async def test_instrument_gets_systems_from_the_channel(tmp_path):
    profiles = Profiles(tmp_path)
    chunks = [chunk("2026-08-01", "measured LaSbTe again") for _ in range(5)]
    p = await build_profile(
        None, ScriptedCompleter("Ran spectroscopy."), profiles,
        name="tesla", kind=INSTRUMENT, channel="CEPBVLQBW", chunks=chunks, today=TODAY,
    )
    assert "LaSbTe" in p.systems


async def test_published_abstract_is_kept_not_overwritten(tmp_path):
    """The lab page states the Besocke head's limited Z-range beside its drift
    resistance; generated copy drops the caveat and keeps the boast."""
    profiles = Profiles(tmp_path)
    seed = "The Createc is a 4-Kelvin STM. Its 3-legged head limits Z-range."
    p = await build_profile(
        None, ScriptedCompleter("An entry."), profiles,
        name="createc", kind=INSTRUMENT, chunks=[chunk("2026-08-01", "x")],
        seed_abstract=seed, source="https://lair.phas.ubc.ca/instruments/createc/",
        today=TODAY,
    )
    assert p.abstract == seed
    assert p.source.startswith("https://lair.phas.ubc.ca")


async def test_a_failed_summary_does_not_write_an_empty_entry(tmp_path):
    class Boom:
        async def complete(self, system, user):
            raise RuntimeError("provider down")

    profiles = Profiles(tmp_path)
    p = await build_profile(None, Boom(), profiles, name="X", kind=PERSON,
                            chunks=[chunk("2026-08-01", "x")], today=TODAY)
    assert p.timeline == []


def test_sized_hardware_designators_are_excluded():
    """DN40, TIC500, CF63 — listing every size is hopeless, so the stem is
    what gets checked."""
    texts = ["fitted the DN40 to the CF63, TIC500 controller reads fine"] * 10
    assert mine_materials(texts) == []


def test_formulae_ending_in_digits_still_survive_the_stem_check():
    texts = ["NbSe2 and PtSn4 and Sr2IrO4"] * 5
    got = {m.formula for m in mine_materials(texts)}
    assert {"NbSe2", "PtSn4", "Sr2IrO4"} <= got


def test_the_labs_own_name_is_not_a_sample_system():
    """LAIR parses as La-I-R. So does BTW as B-T-W."""
    texts = ["LAIR meeting, BTW the sample is ready"] * 10
    assert mine_materials(texts) == []


async def test_an_endorsed_abstract_is_never_regenerated(tmp_path):
    """Endorsement records that a person corrected their own description.
    Regenerating over it would make the button decorative."""
    profiles = Profiles(tmp_path)
    p = Profile(name="X", kind=PERSON, abstract="Hand-written by me.",
                endorsed_by="Dong Chen (2026-08-19)")
    p.add_entry("2025", "old entry")
    profiles.save(p)

    out = await build_profile(
        None, ScriptedCompleter("New entry.", "A REGENERATED ABSTRACT"), profiles,
        name="X", kind=PERSON, chunks=[chunk("2026-08-01", "new material")],
        today=TODAY,
    )
    assert out.abstract == "Hand-written by me."
    assert out.entry_for("2026-08") is not None   # timeline still grows
