from __future__ import annotations

import os
import time

from slackqa.skills import Skill, strip_frontmatter

SAMPLE = """\
---
name: answering-lair-questions
description: Domain guidance for LAIR.
---

Beast is an instrument, not a mood.
"""


def test_strip_frontmatter():
    assert strip_frontmatter(SAMPLE) == "Beast is an instrument, not a mood."


def test_strip_frontmatter_leaves_plain_markdown_alone():
    assert strip_frontmatter("# Title\n\nbody") == "# Title\n\nbody"


def test_strip_frontmatter_only_removes_the_leading_block():
    text = "---\nname: x\n---\n\nbody\n\n---\n\nmore body"
    out = strip_frontmatter(text)
    assert out.startswith("body")
    assert "more body" in out


def test_body_excludes_metadata(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text(SAMPLE)
    skill = Skill(p)
    # name/description exist to help humans and tools find the skill; sending
    # them to the model would spend tokens on nothing.
    assert "description:" not in skill.body
    assert "Beast is an instrument" in skill.body


def test_missing_file_is_empty_not_fatal(tmp_path):
    skill = Skill(tmp_path / "nope.md")
    assert skill.body == ""
    assert bool(skill) is False


def test_reloads_when_file_changes(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: x\n---\n\nfirst")
    skill = Skill(p)
    assert skill.body == "first"

    p.write_text("---\nname: x\n---\n\nsecond")
    os.utime(p, (time.time() + 1, time.time() + 1))
    # Editing guidance should take effect on the next question, not the next
    # restart.
    assert skill.body == "second"


def test_unchanged_file_is_not_re_read(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: x\n---\n\nbody")
    skill = Skill(p)
    skill.reload()
    assert skill.reload() is False


def test_deleted_file_degrades_quietly(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: x\n---\n\nbody")
    skill = Skill(p)
    assert skill.body == "body"
    p.unlink()
    assert skill.body == ""


# --------------------------------------------------------- prompt assembly


def test_skill_reaches_the_system_prompt(tmp_path):
    from slackqa.answerer import SYSTEM_PROMPT, Answerer

    p = tmp_path / "SKILL.md"
    p.write_text(SAMPLE)
    a = Answerer(None, None, team_url="https://x.slack.com", skill=Skill(p))
    system = a._system()
    assert SYSTEM_PROMPT in system          # base rules survive
    assert "Beast is an instrument" in system
    assert "description:" not in system


def test_no_skill_leaves_the_prompt_untouched():
    from slackqa.answerer import SYSTEM_PROMPT, Answerer

    a = Answerer(None, None, team_url="https://x.slack.com")
    assert a._system() == SYSTEM_PROMPT


def test_empty_skill_file_adds_no_header(tmp_path):
    from slackqa.answerer import SYSTEM_PROMPT, Answerer

    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: x\n---\n")
    a = Answerer(None, None, team_url="https://x.slack.com", skill=Skill(p))
    assert a._system() == SYSTEM_PROMPT


# ------------------------------------------------------- the shipped skill


def test_shipped_skill_is_valid_and_covers_the_instruments():
    import pathlib

    p = pathlib.Path("skills/answering/SKILL.md")
    assert p.exists(), "the shipped skill must be present"
    raw = p.read_text()
    assert raw.startswith("---"), "must carry SKILL.md frontmatter"
    assert "name:" in raw and "description:" in raw

    body = strip_frontmatter(raw)
    # Nicknames read as ordinary English and are unguessable as hardware.
    for name in ("Beast", "Tesla", "Omi", "Joel the Jeol", "Createc", "4-probe"):
        assert name in body, f"instrument {name!r} missing from the skill"


def test_shipped_skill_covers_observed_question_types():
    import pathlib

    body = strip_frontmatter(pathlib.Path("skills/answering/SKILL.md").read_text())
    # Each of these appeared in the real query log.
    for topic in ("Status of a thing", "Evidence collection", "Confident recall",
                  "Coined names", "Questions about the index itself"):
        assert topic in body, f"question type {topic!r} not covered"
