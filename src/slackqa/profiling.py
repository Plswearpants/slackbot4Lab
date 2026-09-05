"""Build entity profiles from what the channels actually recorded.

A person's profile is drawn from the conversations they took part in *and* the
ones that name them without them present. That second set is only about a tenth
of the material and carries most of the weight: role, responsibility and
expertise are things colleagues state about someone, not things people claim
about themselves.

History is summarised a year at a time and recent months one at a time, which
matches how the value decays — what someone did in 2019 is worth a paragraph,
what they did last month is worth the detail.

Instrument abstracts are seeded from the lab's published instrument pages rather
than generated. The published prose is better: the Createc page states the
Besocke head's limited Z-range in the same breath as its drift resistance, and a
model writing from chat reliably drops the caveat and keeps the boast. What the
pages do not carry is which materials each instrument actually studies — the
Omicron page names none at all — so that section is mined from the channel.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date

from slackqa.profiles import INSTRUMENT, PERSON, Profile, Profiles, condense

logger = logging.getLogger(__name__)

# Chemical formulae: two or more element-like tokens, or one with digits.
_FORMULA_RE = re.compile(r"\b((?:[A-Z][a-z]?\d*){2,})\b")
_SLACK_ID_RE = re.compile(r"^[UWC][A-Z0-9]{6,}$")

# Acronyms that look like formulae. Techniques, hardware, units and chatter.
_NOT_A_MATERIAL = frozenset(
    """
    STM AFM SPM UHV LEED ARPES XRD XPS EDX EDS QMS RGA TSP STS STML CDW QPI
    LDOS DOS DFT SOC FFT IETS KPFM NcAFM SEM TEM MBE CVD PLD LEEM NEXAFS
    HOMO LUMO CCD PID USB BNC SMA DN DIN ISO PDF URL HTTP HTTPS PNG JPEG
    LHe LN CAD CFI UBC QMI SBQMI NSERC PREVAC ANCORP SAES VAT AKOM NGDA
    DN CF ISO NW KF TIC MKS
    LAIR BTW RTI IIRC AFAIK ASAP EOD PTO OOO ETA FAQ RSVP
    IKEA DHL FedEx UPS PhD MSc BSc PI RA TA
    GPa MPa MHz GHz kHz THz mbar RRR FYI TBD TODO USD EUR CHF
    HMAT CGSS MCSBD SBD SNR GND WBS SMP TIC IV II III IV
    """.split()
)


@dataclass
class Material:
    formula: str
    mentions: int


def mine_materials(texts: list[str], limit: int = 12, floor: int = 3) -> list[Material]:
    """Chemical systems named in a channel, most-discussed first.

    Slack user ids match the same shape as a formula (``U8JQ4HFV3`` reads as
    elements), so they are excluded explicitly rather than by hoping the
    frequency filter hides them.
    """
    counts: Counter[str] = Counter()
    for text in texts:
        for token in _FORMULA_RE.findall(text):
            # Hardware designators carry a size: DN40, TIC500, CF63. Listing
            # every size is hopeless, so the alphabetic stem is checked too.
            stem = token.rstrip("0123456789")
            if (
                len(token) < 3
                or token in _NOT_A_MATERIAL
                or stem in _NOT_A_MATERIAL
                or _SLACK_ID_RE.match(token)
            ):
                continue
            # A bare capitalised word is not a formula; require a digit or a
            # second capital, which is what distinguishes NbSe2 from Monday.
            if not any(c.isdigit() for c in token) and sum(c.isupper() for c in token) < 2:
                continue
            counts[token] += 1
    return [Material(f, n) for f, n in counts.most_common(limit) if n >= floor]


_PERSON_PROMPT = """\
Write a profile entry for {name}, a member of a scanning-probe physics lab,
covering {period}.

Below are excerpts from the group's Slack: conversations {name} took part in,
and conversations where colleagues discussed them.

Write {length}. Cover what they worked on, which instruments and samples, what
they built, fixed or measured, and anything colleagues said about their role.
Name instruments and materials explicitly. Report only what the excerpts
support — where they are thin, say less rather than padding.

Write about the work, never about the excerpts: "a quiet month" is fine, "given
the limited excerpts" is not — a reader of this profile cannot see them.

No preamble, no heading, no bullet points. Prose only.

Excerpts:
{excerpts}
"""

_ABSTRACT_PROMPT = """\
Write a short abstract for {name}, a member of a scanning-probe physics lab.

Below is their profile timeline, oldest last. Distil it into three or four
sentences covering: their role in the group, which instruments they work on,
which sample systems, and what they are expert in. Present tense for what is
current, past tense for what has ended.

This is the answer to "who is this person" for someone who has never met them.
No preamble, no heading. Prose only.

Timeline:
{timeline}
"""

_INSTRUMENT_PERIOD_PROMPT = """\
Write a profile entry for the {name}, an instrument in a scanning-probe physics
lab, covering {period}.

Below are excerpts from its Slack channel. Write {length} about what happened to
the instrument: what was built, installed, repaired, broken, upgraded or
measured, and who did it. Prefer concrete specifics — parts, temperatures,
pressures, part numbers — over general statements.

Write about the instrument, never about the excerpts — a reader of this profile
cannot see them.

No preamble, no heading. Prose only.

Excerpts:
{excerpts}
"""


def _fmt(chunks: list[dict], cap: int = 60) -> str:
    return "\n\n---\n\n".join(c["text"][:1200] for c in chunks[:cap])


async def summarise_period(
    completer,
    name: str,
    period: str,
    chunks: list[dict],
    *,
    kind: str = PERSON,
    monthly: bool = False,
) -> str:
    """One timeline entry. Returns "" when nothing usable came back."""
    if not chunks:
        return ""
    length = "one or two sentences" if monthly else "a single paragraph"
    template = _PERSON_PROMPT if kind == PERSON else _INSTRUMENT_PERIOD_PROMPT
    prompt = template.format(
        name=name, period=period, length=length, excerpts=_fmt(chunks)
    )
    try:
        text = (await completer.complete("", prompt)).strip()
    except Exception:
        logger.exception("Profile entry failed for %s %s", name, period)
        return ""
    return " ".join(text.split())


async def write_abstract(completer, profile: Profile) -> str:
    if not profile.timeline:
        return ""
    timeline = "\n".join(f"{e.period}: {e.text}" for e in profile.timeline[:20])
    try:
        return " ".join(
            (await completer.complete(
                "", _ABSTRACT_PROMPT.format(name=profile.name, timeline=timeline)
            )).strip().split()
        )
    except Exception:
        logger.exception("Abstract failed for %s", profile.name)
        return profile.abstract


def periods_to_build(
    chunks_by_period: dict[str, list[dict]],
    existing: Profile | None,
    today: date | None = None,
) -> list[tuple[str, bool]]:
    """Which periods still need writing, as (period, monthly).

    Recent months are built monthly and older material yearly, so a first run
    over nine years costs one call per person-year rather than one per month.
    """
    today = today or date.today()
    have = {e.period for e in existing.timeline} if existing else set()
    out: list[tuple[str, bool]] = []
    for period in sorted(chunks_by_period):
        if period in have:
            continue
        out.append((period, len(period) == 7))
    return out


def group_chunks(chunks: list[dict], today: date | None = None, months: int = 6) -> dict:
    """Bucket chunks by year, except recent ones which bucket by month."""
    from datetime import datetime

    today = today or date.today()
    cutoff = (today.year * 12 + today.month) - months
    buckets: dict[str, list[dict]] = {}
    for c in chunks:
        when = datetime.fromtimestamp(c["start_ts"], tz=UTC)
        key = (
            f"{when.year:04d}-{when.month:02d}"
            if when.year * 12 + when.month > cutoff
            else f"{when.year:04d}"
        )
        buckets.setdefault(key, []).append(c)
    return buckets


async def build_profile(
    store,
    completer,
    profiles: Profiles,
    *,
    name: str,
    kind: str,
    chunks: list[dict],
    slack_id: str = "",
    channel: str = "",
    seed_abstract: str = "",
    source: str = "",
    today: date | None = None,
    max_periods: int | None = None,
) -> Profile:
    """Create or extend one profile. Only missing periods are generated."""
    profile = profiles.load(name, kind) or Profile(
        name=name, kind=kind, slack_id=slack_id, channel=channel
    )
    profile.slack_id = profile.slack_id or slack_id
    profile.channel = profile.channel or channel
    if seed_abstract and not profile.abstract:
        # Published lab copy beats anything generated from chat, and the source
        # is recorded so a reader can tell which is which.
        profile.abstract = seed_abstract
        profile.source = source

    buckets = group_chunks(chunks, today)
    todo = periods_to_build(buckets, profile, today)
    if max_periods:
        todo = todo[-max_periods:]

    for period, monthly in todo:
        text = await summarise_period(
            completer, name, period, buckets[period], kind=kind, monthly=monthly
        )
        if text:
            profile.add_entry(period, text)
            logger.info("  %s %s: %d chars", name, period, len(text))

    condense(profile, today)

    if kind == INSTRUMENT:
        materials = mine_materials([c["text"] for c in chunks])
        if materials:
            profile.systems = ", ".join(m.formula for m in materials)
    # An endorsed abstract is somebody's considered description of themselves
    # or their instrument. Regenerating it would make endorsement decorative
    # and quietly discard the correction it recorded.
    if profile.endorsed:
        logger.info("%s is endorsed; leaving the abstract alone", profile.name)
    elif not profile.abstract or (todo and kind == PERSON):
        written = await write_abstract(completer, profile)
        if written:
            profile.abstract = written

    profile.updated = (today or date.today()).isoformat()
    profiles.save(profile)
    return profile
