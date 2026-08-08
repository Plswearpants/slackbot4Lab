from __future__ import annotations

from datetime import date

from slackqa.glossary import Entry, Glossary, parse, render_html, render_markdown

SAMPLE = """\
# Glossary

Preamble that must not become an entry.

## 4-probe

The four-probe STM in the lab; four independent tips on one sample.

- aliases: four probe, 4probe
- endorsed-by: Dong Chen (2026-08-06)

## beast

The older breakout box, used as the wiring reference.

- drafted: agent (2026-08-05)
"""


def test_parse_entries():
    e = parse(SAMPLE)
    assert [x.term for x in e] == ["4-probe", "beast"]
    assert e[0].aliases == ["four probe", "4probe"]
    assert e[0].endorsed is True
    assert e[1].endorsed is False
    assert e[1].drafted == "agent (2026-08-05)"


def test_preamble_is_not_an_entry():
    assert all("Preamble" not in x.term for x in parse(SAMPLE))


def test_definition_captured():
    assert "four independent tips" in parse(SAMPLE)[0].definition


def test_roundtrip_preserves_content():
    first = parse(SAMPLE)
    second = parse(render_markdown(first))
    assert [(x.term, x.aliases, x.endorsed_by) for x in first] == [
        (y.term, y.aliases, y.endorsed_by) for y in second
    ]


def test_unknown_metadata_survives_roundtrip():
    # A human may add their own keys; the agent must not eat them on rewrite.
    entries = parse("## x\n\ndef\n\n- owner: sarah\n")
    assert entries[0].extra == {"owner": "sarah"}
    assert "owner: sarah" in render_markdown(entries)


def test_empty_file():
    assert parse("") == []


# ------------------------------------------------------------------ accessors


def gl(tmp_path, text=SAMPLE) -> Glossary:
    p = tmp_path / "glossary.md"
    p.write_text(text)
    return Glossary.load(p)


def test_get_by_term_and_alias(tmp_path):
    g = gl(tmp_path)
    assert g.get("4-probe").term == "4-probe"
    assert g.get("4PROBE").term == "4-probe"
    assert g.get("four probe").term == "4-probe"
    assert g.get("nonexistent") is None


def test_add_rejects_duplicate_including_alias(tmp_path):
    g = gl(tmp_path)
    assert g.add(Entry(term="new thing", definition="d")) is True
    assert g.add(Entry(term="4-probe", definition="dup")) is False
    assert g.add(Entry(term="4probe", definition="dup via alias")) is False


def test_endorse(tmp_path):
    g = gl(tmp_path)
    assert g.endorse("beast", "Sarah", date(2026, 8, 6)) is True
    assert g.get("beast").endorsed_by == "Sarah (2026-08-06)"
    assert g.endorse("missing", "Sarah") is False


def test_save_and_reload(tmp_path):
    g = gl(tmp_path)
    g.add(Entry(term="QMI", definition="Quantum Matter Institute"))
    g.save()
    assert Glossary.load(g.path).get("QMI").definition == "Quantum Matter Institute"


def test_load_missing_file_is_empty(tmp_path):
    assert Glossary.load(tmp_path / "nope.md").entries == []


# ------------------------------------------------------------------- matching


def test_detect_finds_term_and_alias(tmp_path):
    g = gl(tmp_path)
    assert [e.term for e in g.detect("what is the 4-probe status?")] == ["4-probe"]
    assert [e.term for e in g.detect("how is 4probe doing")] == ["4-probe"]


def test_detect_respects_word_boundaries(tmp_path):
    g = gl(tmp_path)
    # "beast" must not fire on "beastly", and a bare "probe" must not match.
    assert g.detect("that was beastly hard") == []
    assert g.detect("we used a probe") == []


def test_detect_is_case_insensitive(tmp_path):
    assert [e.term for e in gl(tmp_path).detect("THE BEAST broke")] == ["beast"]


def test_detect_multiple(tmp_path):
    assert len(gl(tmp_path).detect("did the beast work with the 4-probe?")) == 2


def test_detect_prefers_longer_terms(tmp_path):
    g = gl(tmp_path, "## box\n\nA box.\n\n## breakout box\n\nThe breakout box.\n")
    assert g.detect("the breakout box")[0].term == "breakout box"


def test_prompt_block_marks_endorsement(tmp_path):
    g = gl(tmp_path)
    block = g.prompt_block(g.detect("4-probe and beast"))
    assert "endorsed by Dong Chen" in block
    assert "UNENDORSED" in block
    assert "provisional" in block


def test_prompt_block_empty_for_no_matches(tmp_path):
    assert gl(tmp_path).prompt_block([]) == ""


def test_query_expansion_pulls_definition_terms(tmp_path):
    g = gl(tmp_path)
    expansion = g.query_expansion(g.detect("4-probe"))
    assert "four" in expansion.lower() or "STM" in expansion
    assert len(expansion.split()) <= 12


# ----------------------------------------------------------------------- html


def test_html_distinguishes_endorsement(tmp_path):
    out = render_html(gl(tmp_path).entries)
    assert "badge ok" in out and "badge prov" in out
    assert "1 endorsed" in out and "1 awaiting review" in out


def test_html_escapes_content():
    out = render_html([Entry(term="<script>", definition="a & b")])
    assert "<script>" not in out.split("<style>")[1]
    assert "&lt;script&gt;" in out and "a &amp; b" in out


def test_html_handles_empty():
    assert "No terms yet" in render_html([])


def test_plural_and_singular_are_one_term(tmp_path):
    # Mining proposed "heat shield" and "heat shields" and wrote two entries
    # with conflicting details for the same object.
    g = gl(tmp_path, "## heat shield\n\nNested radiation shields.\n")
    assert g.get("heat shields") is not None
    assert g.add(Entry(term="heat shields", definition="dup")) is False


def test_plural_folding_leaves_short_words_alone(tmp_path):
    g = gl(tmp_path, "## lens\n\nAn optic.\n")
    assert g.get("len") is None
    assert g.get("lens") is not None


def test_normalize_term():
    from slackqa.glossary import normalize_term

    assert normalize_term("Heat Shields") == "heat shield"
    assert normalize_term("assemblies") == "assembly"
    assert normalize_term("UHV") == "uhv"


# ------------------------------------------------------------ channel scoping

SCOPED = """\
## breakout box

A 12x6x4 box adapting two D25 connectors to 36 BNC cables.

- kind: instrument
- channels: C4PROBE

## breakout box

The box in "beast", sharing common ground with the chamber.

- kind: instrument
- channels: CCRETEC

## UHV

Ultra-high vacuum, used across the lab.

- kind: phenomenon
"""


def test_same_term_can_mean_different_things_per_channel(tmp_path):
    g = gl(tmp_path, SCOPED)
    assert "D25" in g.get("breakout box", "C4PROBE").definition
    assert "beast" in g.get("breakout box", "CCRETEC").definition


def test_scoped_entry_is_invisible_to_other_channels(tmp_path):
    g = gl(tmp_path, SCOPED)
    assert g.detect("the breakout box", "C4PROBE")[0].definition.startswith("A 12x6x4")
    assert "beast" in g.detect("the breakout box", "CCRETEC")[0].definition
    assert g.detect("the breakout box", "COTHER") == []


def test_unscoped_entry_applies_everywhere(tmp_path):
    g = gl(tmp_path, SCOPED)
    for ch in ("C4PROBE", "CCRETEC", "COTHER"):
        assert [e.term for e in g.detect("is the UHV ok", ch)] == ["UHV"]


def test_add_allows_same_term_in_a_different_channel(tmp_path):
    g = gl(tmp_path, SCOPED)
    assert g.add(Entry(term="breakout box", definition="third", channels=["CTHIRD"]))
    assert g.add(Entry(term="breakout box", definition="dup", channels=["C4PROBE"])) is False


def test_channel_specific_entry_shadows_a_global_one(tmp_path):
    g = gl(tmp_path, "## widget\n\nGeneric widget.\n\n## widget\n\nOur widget.\n\n- channels: C1\n")
    assert g.detect("the widget", "C1")[0].definition == "Our widget."
    assert len(g.detect("the widget", "C1")) == 1
    assert g.detect("the widget", "C2")[0].definition == "Generic widget."


def test_channels_survive_roundtrip(tmp_path):
    g = gl(tmp_path, SCOPED)
    from slackqa.glossary import parse, render_markdown

    again = parse(render_markdown(g.entries))
    assert sorted(e.channels for e in again) == [[], ["C4PROBE"], ["CCRETEC"]]


def test_html_shows_scope(tmp_path):
    out = render_html(gl(tmp_path, SCOPED).entries, channel_names={"C4PROBE": "4probe"})
    assert "#4probe" in out
    assert "all channels" in out


def test_query_expansion_leads_with_the_term_itself(tmp_path):
    # The bug this exists to prevent: expansion drawn only from the definition
    # dropped "XRD" — the one token a question saying "X-ray spectroscopy"
    # needed to reach chunks that only ever write the acronym.
    g = gl(tmp_path, "## XRD\n\nX-ray diffraction for material identification.\n")
    expansion = g.query_expansion(g.detect("XRD"))
    assert expansion.split()[0] == "XRD"


def test_query_expansion_includes_aliases(tmp_path):
    g = gl(
        tmp_path,
        "## XRD\n\nDiffraction.\n\n- aliases: XPS, EDX\n",
    )
    expansion = g.query_expansion(g.detect("XRD")).split()
    assert {"XRD", "XPS", "EDX"} <= set(expansion)
