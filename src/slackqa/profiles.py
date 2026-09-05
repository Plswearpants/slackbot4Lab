"""Entity profiles: what a person or instrument has been about, over time.

The glossary says what a thing *is* and does not change; a profile says how it
has *gone* and changes constantly. Keeping them apart is what stops the two
drifting into contradicting each other about the same heat shield.

A profile has two zones:

* an **abstract** — role, systems, expertise for a person; specification and
  purpose for an instrument — rewritten on each pass so "who is this" stays
  answerable in one screen;
* a **timeline** — append-only, dated, at rolling resolution. The last six
  months carry one entry per month; everything older is one paragraph per year.
  On each pass, months that have aged past six are folded into their year.

Folding is lossy, and that is safe here only because the source messages remain
indexed forever: a profile is a view over the record, never the record itself,
so any condensed period can be regenerated at full resolution.

Instrument abstracts are seeded from the lab's published instrument pages, which
are better than anything generated from chat — the Createc page states the
Besocke head's limited Z-range alongside its drift resistance, and generated
copy reliably drops the caveat. ``source`` records where seeded text came from.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

PERSON = "person"
INSTRUMENT = "instrument"

# Months kept at monthly resolution before folding into the year.
FINE_GRAIN_MONTHS = 6

_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_ENTRY_RE = re.compile(r"^###\s+(\d{4}(?:-\d{2})?)\s*$")
_META_RE = re.compile(r"^\s*[-*]\s+([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")


@dataclass
class Entry:
    """One dated slice of the timeline. ``period`` is YYYY or YYYY-MM.

    Endorsement lives here rather than only on the profile because a ten-year
    timeline is not one claim: someone may vouch for last month and dispute
    2019, and a single profile-level button cannot say that.
    """

    period: str
    text: str
    endorsed_by: str | None = None
    edited_by: str | None = None

    @property
    def endorsed(self) -> bool:
        return bool(self.endorsed_by)

    @property
    def reviewed(self) -> bool:
        """Touched by a person, either way — so regeneration leaves it alone."""
        return bool(self.endorsed_by or self.edited_by)

    @property
    def monthly(self) -> bool:
        return len(self.period) == 7

    @property
    def year(self) -> str:
        return self.period[:4]

    def age_months(self, today: date | None = None) -> int:
        today = today or date.today()
        y = int(self.year)
        m = int(self.period[5:7]) if self.monthly else 12
        return (today.year - y) * 12 + (today.month - m)


@dataclass
class Profile:
    name: str
    kind: str = PERSON
    abstract: str = ""
    systems: str = ""
    timeline: list[Entry] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    channel: str = ""
    slack_id: str = ""
    source: str = ""
    endorsed_by: str | None = None
    updated: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def endorsed(self) -> bool:
        return bool(self.endorsed_by)

    @property
    def names(self) -> list[str]:
        return [self.name, *self.aliases]

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.split() else self.name

    def last_active(self) -> str:
        """Period of the most recent timeline entry, or ""."""
        return max((e.period for e in self.timeline), default="")

    def active(self, months: int = FINE_GRAIN_MONTHS, today: date | None = None) -> bool:
        """A person is active if they have shown up inside the window.

        Instruments are always active: a microscope that nobody mentioned this
        month has not left the lab.
        """
        if self.kind == INSTRUMENT:
            return True
        if not self.timeline:
            return False
        return min(e.age_months(today) for e in self.timeline) < months

    def entry_for(self, period: str) -> Entry | None:
        return next((e for e in self.timeline if e.period == period), None)

    def add_entry(
        self,
        period: str,
        text: str,
        *,
        endorsed_by: str | None = None,
        edited_by: str | None = None,
    ) -> None:
        """Add or replace one period's entry, keeping the timeline sorted."""
        existing = self.entry_for(period)
        if existing:
            existing.text = text
            if endorsed_by is not None:
                existing.endorsed_by = endorsed_by
            if edited_by is not None:
                existing.edited_by = edited_by
        else:
            self.timeline.append(Entry(period, text, endorsed_by, edited_by))
        self.timeline.sort(key=lambda e: e.period, reverse=True)

    def recent(
        self, months: int = FINE_GRAIN_MONTHS, today: date | None = None
    ) -> list[Entry]:
        return [e for e in self.timeline if e.age_months(today) < months]


def condense(
    profile: Profile,
    today: date | None = None,
    months: int = FINE_GRAIN_MONTHS,
) -> list[str]:
    """Fold aged monthly entries into their year. Returns the years touched.

    Only monthly entries older than the window are folded, and an existing
    yearly paragraph absorbs them rather than being replaced — the year's text
    is the accumulation, not the newest fragment.
    """
    # A reviewed entry is never folded. Condensation is otherwise safe because
    # nothing is lost — the source messages remain, so any period can be
    # regenerated at full resolution. A person's endorsement is not in those
    # messages: it exists only here, and folding would destroy it silently.
    aged = [
        e
        for e in profile.timeline
        if e.monthly and e.age_months(today) >= months and not e.reviewed
    ]
    if not aged:
        return []

    touched: list[str] = []
    for year in sorted({e.year for e in aged}):
        months_in_year = sorted(
            (e for e in aged if e.year == year), key=lambda e: e.period
        )
        existing = profile.entry_for(year)
        parts = [existing.text] if existing and existing.text else []
        parts += [e.text for e in months_in_year]
        merged = " ".join(" ".join(parts).split())

        # Only the aged entries are folded. Removing every month of the year
        # would swallow the recent ones too, so a January fold in August would
        # take July and August with it.
        folded = {e.period for e in months_in_year}
        profile.timeline = [e for e in profile.timeline if e.period not in folded]
        profile.add_entry(year, merged)
        touched.append(year)
    return touched


# ------------------------------------------------------------------- parsing


def parse(text: str) -> Profile:
    profile = Profile(name="")
    section: str | None = None
    period: str | None = None
    buffer: list[str] = []
    body: dict[str, list[str]] = {"abstract": [], "systems": []}

    entry_meta: dict[str, str] = {}

    def flush_entry() -> None:
        nonlocal period, buffer, entry_meta
        if period is not None:
            profile.add_entry(
                period,
                " ".join(" ".join(buffer).split()),
                endorsed_by=entry_meta.get("endorsed-by"),
                edited_by=entry_meta.get("edited-by"),
            )
        period, buffer, entry_meta = None, [], {}

    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            profile.name = heading.group(1).strip()
            continue

        entry = _ENTRY_RE.match(line)
        if entry:
            flush_entry()
            period = entry.group(1)
            continue

        sec = _SECTION_RE.match(line)
        if sec:
            flush_entry()
            section = sec.group(1).strip().lower()
            continue

        meta = _META_RE.match(line)
        if meta and period is not None:
            key = meta.group(1).lower().replace("_", "-")
            if key in ("endorsed-by", "edited-by"):
                entry_meta[key] = meta.group(2).strip()
                continue
        if meta and period is None:
            key, value = meta.group(1).lower(), meta.group(2).strip()
            if key == "kind":
                profile.kind = value or PERSON
            elif key == "aliases":
                profile.aliases = [a.strip() for a in value.split(",") if a.strip()]
            elif key == "channel":
                profile.channel = value
            elif key in ("slack-id", "slack_id"):
                profile.slack_id = value
            elif key == "source":
                profile.source = value
            elif key in ("endorsed-by", "endorsed_by"):
                profile.endorsed_by = value or None
            elif key == "updated":
                profile.updated = value
            else:
                profile.extra[key] = value
            continue

        if period is not None:
            buffer.append(line)
        elif section in body:
            body[section].append(line)

    flush_entry()
    profile.abstract = "\n".join(body["abstract"]).strip()
    profile.systems = "\n".join(body["systems"]).strip()
    return profile


def render(profile: Profile) -> str:
    out = [f"# {profile.name}", ""]
    meta = [f"- kind: {profile.kind}"]
    if profile.slack_id:
        meta.append(f"- slack-id: {profile.slack_id}")
    if profile.channel:
        meta.append(f"- channel: {profile.channel}")
    if profile.aliases:
        meta.append(f"- aliases: {', '.join(profile.aliases)}")
    if profile.source:
        meta.append(f"- source: {profile.source}")
    if profile.endorsed_by:
        meta.append(f"- endorsed-by: {profile.endorsed_by}")
    if profile.updated:
        meta.append(f"- updated: {profile.updated}")
    meta += [f"- {k}: {v}" for k, v in sorted(profile.extra.items())]
    out += meta + [""]

    out += ["## Abstract", "", profile.abstract.strip() or "_Not yet written._", ""]
    if profile.systems.strip() or profile.kind == INSTRUMENT:
        out += ["## Systems", "", profile.systems.strip() or "_None recorded._", ""]

    out += ["## Timeline", ""]
    for e in profile.timeline:
        out += [f"### {e.period}", "", e.text.strip(), ""]
        marks = []
        if e.endorsed_by:
            marks.append(f"- endorsed-by: {e.endorsed_by}")
        if e.edited_by:
            marks.append(f"- edited-by: {e.edited_by}")
        if marks:
            out += marks + [""]
    return "\n".join(out).rstrip() + "\n"


# First names that are also ordinary English words. Matching "Will Ho" on a
# bare "will" would fire on a large share of every question asked, so these
# require the full name or an explicit @-mention.
COMMON_WORD_NAMES = frozenset(
    """will mark may art bill rose dawn hope faith joy guy frank drew chase
    hunter grant summer sky shun june rich jack russell miles wade victor
    curtis daisy grace olive page ray reed sunny""".split()
)


def _first_name_used(text: str, first: str) -> bool:
    """Whether ``first`` is used as a name here rather than as a word.

    Two guards, because either alone leaks: the token must be capitalised as
    written, and a name that is also a common word never matches on its own.
    """
    if first.lower() in COMMON_WORD_NAMES:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(first.capitalize())}(?!\w)", text))


# -------------------------------------------------------------------- store


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "unnamed"


class Profiles:
    """A directory of profile files, one per entity."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _dir(self, kind: str) -> Path:
        return self.root / ("people" if kind == PERSON else "instruments")

    def path_for(self, name: str, kind: str) -> Path:
        return self._dir(kind) / f"{slug(name)}.md"

    def load(self, name: str, kind: str) -> Profile | None:
        path = self.path_for(name, kind)
        if not path.exists():
            return None
        return parse(path.read_text())

    def save(self, profile: Profile) -> Path:
        path = self.path_for(profile.name, profile.kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(profile))
        return path

    def all(self, kind: str | None = None) -> list[Profile]:
        kinds = [kind] if kind else [PERSON, INSTRUMENT]
        out: list[Profile] = []
        for k in kinds:
            d = self._dir(k)
            if not d.exists():
                continue
            for p in sorted(d.glob("*.md")):
                out.append(parse(p.read_text()))
        return out

    def candidates(
        self,
        text: str,
        *,
        evidence: str = "",
        roster: Sequence[str] = (),
    ) -> tuple[list[Profile], list[list[Profile]]]:
        """Profiles named in ``text``, split into the certain and the ambiguous.

        Returns ``(certain, groups)``, each group holding the people one name
        could mean. Most ambiguity resolves here rather than reaching the model:
        if only one Alex appears in the retrieved excerpts or in this channel,
        that is the Alex who was meant.
        """
        # Handle -> the profiles it could refer to. A handle is a full name, a
        # declared alias, or a person's first name.
        found: dict[str, list[Profile]] = {}

        # Full names and declared aliases first. An alias is a deliberate human
        # statement ("Alexander also goes by Alex"), so it is trusted as
        # written and matched case-insensitively.
        residual = text
        for prof in sorted(self.all(), key=lambda x: -len(x.name)):
            for n in prof.names:
                n = n.strip()
                if n and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", residual, re.I):
                    found.setdefault(n.lower(), []).append(prof)

        # Remove full names already matched, so "Alex Tubby fixed it" does not
        # also register a bare "Alex" and turn a full name back into a guess.
        # Single-token handles are left in place: stripping a declared alias
        # would hide the ambiguity it was declared to create.
        for handle in [h for h in found if " " in h]:
            residual = re.sub(rf"(?<!\w){re.escape(handle)}(?!\w)", " ", residual, flags=re.I)

        # Then bare first names, under stricter rules — these are inferred
        # rather than declared.
        for prof in self.all(PERSON):
            first = prof.first_name
            if first.lower() == prof.name.lower():
                continue
            if _first_name_used(residual, first):
                found.setdefault(first.lower(), []).append(prof)

        certain: list[Profile] = []
        groups: list[list[Profile]] = []
        haystack = f"{evidence}\n{' '.join(roster)}"
        def dedupe(ps: list[Profile]) -> list[Profile]:
            out: dict[str, Profile] = {}
            for x in ps:
                out.setdefault(x.name, x)
            return list(out.values())

        for _handle, people in sorted(found.items()):
            people = dedupe(people)
            if len(people) == 1:
                certain.append(people[0])
                continue
            supported = [
                p
                for p in people
                if re.search(rf"(?<!\w){re.escape(p.name)}(?!\w)", haystack, re.I)
            ]
            if len(supported) == 1:
                certain.append(supported[0])
            else:
                groups.append(supported or people)
        return dedupe(certain), groups

    def detect(self, text: str) -> list[Profile]:
        """Profiles whose name or alias appears in ``text``.

        Word-boundary matched and longest-first, so "Beast" does not fire on
        "beastly" and a two-word name wins over one of its own words.
        """
        found: list[Profile] = []
        for p in sorted(self.all(), key=lambda x: -len(x.name)):
            for n in p.names:
                n = n.strip()
                if n and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text, re.IGNORECASE):
                    found.append(p)
                    break
        return found
