"""Shared vocabulary: a Markdown glossary the agent and humans both edit.

Markdown is the source of truth and ``glossary.html`` is a generated view. An
HTML source would have to be parsed and rewritten by the agent on every mined
term, and one slightly-off hand edit could silently break that parse; Markdown
degrades gracefully and diffs readably.

Entries carry provenance rather than sitting behind an approval gate. A mined
definition is usable immediately but marked unendorsed, and that state travels
into the prompt so the model hedges instead of stating it flatly. Endorsing an
entry is a one-line edit, by a person, naming the person.

Only two kinds of thing belong here, both channel-specific: **instrument** (a
physical part or apparatus this group builds or uses) and **phenomenon** (a
scientific effect this group studies). Generic vocabulary is deliberately out
of scope — "STM means scanning tunneling microscope" tells the bot nothing it
could not already guess, whereas the dimensions and wiring of *this* group's
breakout box exist nowhere but in this channel.

Entries are scoped to the channel they were mined from. "Breakout box" means a
12" D25-to-BNC adapter in one channel and a differently-grounded box in another,
so a single shared definition would be confidently wrong in one of them. An
entry with no ``channels`` line applies everywhere — that is how genuinely
shared vocabulary is expressed, by deleting the line.

``status`` and ``timeline`` describe a moving target, so they are stamped with
the date they were derived and the model is told to prefer fresher channel
evidence over them.

Format::

    ## breakout box

    A 12" x 6" x 4" box adapting two D25 connectors on the chamber side to
    36 BNC cables on the controller side.

    - kind: instrument
    - channels: C0123ABC
    - status: on build at the electronic shop
    - timeline: expected complete by 2026-08-13
    - as-of: 2026-08-06
    - aliases: breakout-box, BOB
    - endorsed-by: Dong Chen (2026-08-06)

Everything except the heading is optional. Unknown metadata keys are preserved
on rewrite so a human can add their own without the agent eating them.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_META_RE = re.compile(r"^\s*[-*]\s+([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")

_HEADER = """\
# Glossary

Shared vocabulary for this workspace. Both you and the bot edit this file;
`glossary.html` is generated from it and should not be edited directly.

Entries without an `endorsed-by` line were drafted automatically and are used
with a caveat. To endorse one, add a line under it:

    - endorsed-by: Your Name (YYYY-MM-DD)
"""


def normalize_term(term: str) -> str:
    """Fold a term for duplicate detection.

    Mining proposed both "heat shield" and "heat shields" and defined them
    separately, producing two entries for one object with conflicting details.
    Plural folding is naive but the failure mode it prevents is worse than the
    over-merging it risks; short words are left alone so "lens" survives.
    """
    t = term.strip().lower()
    if len(t) > 4:
        t = re.sub(r"(ies)$", "y", t)
        t = re.sub(r"(es|s)$", "", t)
    return t


INSTRUMENT = "instrument"
PHENOMENON = "phenomenon"
KINDS = (INSTRUMENT, PHENOMENON)


@dataclass
class Entry:
    term: str
    definition: str = ""
    kind: str = INSTRUMENT
    status: str | None = None
    timeline: str | None = None
    as_of: str | None = None
    aliases: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    endorsed_by: str | None = None
    drafted: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def endorsed(self) -> bool:
        return bool(self.endorsed_by)

    @property
    def has_volatile(self) -> bool:
        return bool(self.status or self.timeline)

    def stale(self, max_age_days: int, today: date | None = None) -> bool:
        """True when status/timeline were derived long enough ago to re-check."""
        if not self.has_volatile:
            return False
        if not self.as_of:
            return True
        try:
            age = ((today or date.today()) - date.fromisoformat(self.as_of)).days
        except ValueError:
            return True
        return age >= max_age_days

    def applies_to(self, channel_id: str | None) -> bool:
        """An entry with no channels is global; otherwise it must list this one."""
        if not self.channels:
            return True
        return channel_id is None or channel_id in self.channels

    @property
    def names(self) -> list[str]:
        """Term plus aliases — everything that should match in a question."""
        return [self.term, *self.aliases]


# ------------------------------------------------------------------- parsing


def parse(text: str) -> list[Entry]:
    entries: list[Entry] = []
    current: Entry | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None:
            current.definition = "\n".join(body).strip()
            entries.append(current)

    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            current = Entry(term=heading.group(1).strip())
            body = []
            continue
        if current is None:
            continue  # preamble before the first entry
        meta = _META_RE.match(line)
        if meta:
            key, value = meta.group(1).lower(), meta.group(2).strip()
            if key == "aliases":
                current.aliases = [a.strip() for a in value.split(",") if a.strip()]
            elif key == "channels":
                current.channels = [c.strip() for c in value.split(",") if c.strip()]
            elif key in ("endorsed-by", "endorsed_by"):
                current.endorsed_by = value or None
            elif key == "drafted":
                current.drafted = value or None
            elif key == "kind":
                current.kind = value.strip().lower() or INSTRUMENT
            elif key == "status":
                current.status = value or None
            elif key == "timeline":
                current.timeline = value or None
            elif key in ("as-of", "as_of"):
                current.as_of = value or None
            else:
                current.extra[key] = value
            continue
        body.append(line)

    flush()
    return [e for e in entries if e.term]


def render_markdown(entries: list[Entry]) -> str:
    out = [_HEADER]
    for e in sorted(entries, key=lambda x: x.term.lower()):
        out.append(f"## {e.term}\n")
        if e.definition:
            out.append(e.definition.strip() + "\n")
        meta = [f"- kind: {e.kind}"]
        if e.channels:
            meta.append(f"- channels: {', '.join(e.channels)}")
        if e.status:
            meta.append(f"- status: {e.status}")
        if e.timeline:
            meta.append(f"- timeline: {e.timeline}")
        if e.as_of:
            meta.append(f"- as-of: {e.as_of}")
        if e.aliases:
            meta.append(f"- aliases: {', '.join(e.aliases)}")
        if e.endorsed_by:
            meta.append(f"- endorsed-by: {e.endorsed_by}")
        if e.drafted:
            meta.append(f"- drafted: {e.drafted}")
        meta += [f"- {k}: {v}" for k, v in sorted(e.extra.items())]
        if meta:
            out.append("\n".join(meta) + "\n")
    return "\n".join(out).rstrip() + "\n"


class SkipList:
    """Terms mining has already judged out of scope.

    Without this, every pass re-sends the same rejected candidates to the model
    for triage forever. The file is plain text so a human can also veto a term
    permanently by adding a line.
    """

    def __init__(self, path: Path, terms: set[str] | None = None) -> None:
        self.path = Path(path)
        self.terms = terms or set()

    @classmethod
    def load(cls, path: Path | str) -> SkipList:
        path = Path(path)
        if not path.exists():
            return cls(path)
        terms = {
            line.strip().lower()
            for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        }
        return cls(path, terms)

    def __contains__(self, term: str) -> bool:
        return term.strip().lower() in self.terms

    def add(self, *terms: str) -> None:
        self.terms.update(t.strip().lower() for t in terms if t.strip())

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# Terms mining has judged out of scope for this glossary.\n"
            "# Add a line here to permanently stop a term being proposed.\n"
        )
        self.path.write_text(header + "\n".join(sorted(self.terms)) + "\n")


class Glossary:
    def __init__(self, path: Path, entries: list[Entry] | None = None) -> None:
        self.path = Path(path)
        self.entries = entries if entries is not None else []

    @classmethod
    def load(cls, path: Path | str) -> Glossary:
        path = Path(path)
        if not path.exists():
            return cls(path, [])
        return cls(path, parse(path.read_text()))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(render_markdown(self.entries))

    # ------------------------------------------------------------- accessors

    def _matching(self, term: str) -> list[Entry]:
        low = term.strip().lower()
        norm = normalize_term(term)
        out = []
        for e in self.entries:
            names = [n.strip().lower() for n in e.names]
            if low in names or norm in {normalize_term(n) for n in names}:
                out.append(e)
        return out

    def get(self, term: str, channel_id: str | None = None) -> Entry | None:
        """The entry for ``term`` as it applies in ``channel_id``.

        A channel-specific entry beats a global one, so a channel can override
        shared vocabulary without deleting it for everyone else.
        """
        candidates = [e for e in self._matching(term) if e.applies_to(channel_id)]
        if not candidates:
            return None
        scoped = [e for e in candidates if e.channels]
        return scoped[0] if scoped else candidates[0]

    def has(self, term: str, channel_id: str | None = None) -> bool:
        return self.get(term, channel_id) is not None

    def add(self, entry: Entry) -> bool:
        """Add unless an entry already covers this term in the same scope.

        Scope matters: two channels may each legitimately define "breakout box",
        so a global check would wrongly block the second one.
        """
        scope = entry.channels[0] if entry.channels else None
        if self.has(entry.term, scope):
            return False
        self.entries.append(entry)
        return True

    def endorse(self, term: str, who: str, when: date | None = None) -> bool:
        e = self.get(term)
        if e is None:
            return False
        e.endorsed_by = f"{who} ({(when or date.today()).isoformat()})"
        return True

    # -------------------------------------------------------------- matching

    def for_channel(self, channel_id: str | None) -> list[Entry]:
        return [e for e in self.entries if e.applies_to(channel_id)]

    def detect(self, text: str, channel_id: str | None = None) -> list[Entry]:
        """Entries whose term or alias appears in ``text``.

        Word-boundary matching so "4-probe" doesn't fire on "probe" and a term
        never matches inside a longer unrelated word. Longest terms first, so a
        multi-word entry wins over one of its own words.
        """
        found: list[Entry] = []
        pool = self.for_channel(channel_id)
        # A channel-specific entry shadows a global one of the same name.
        scoped_names = {n.lower() for e in pool if e.channels for n in e.names}
        pool = [e for e in pool if e.channels or not any(
            n.lower() in scoped_names for n in e.names
        )]
        for e in sorted(pool, key=lambda x: -len(x.term)):
            for name in e.names:
                name = name.strip()
                if not name:
                    continue
                if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
                    found.append(e)
                    break
        return found

    def prompt_block(self, entries: list[Entry]) -> str:
        """Definitions for the model, carrying endorsement state and staleness.

        Status and timeline are stamped with the date they were derived, and
        the model is told to prefer newer channel evidence. Without that, a
        glossary snapshot would silently outrank a fresher message saying the
        build already shipped.
        """
        if not entries:
            return ""
        lines = []
        for e in entries:
            mark = (
                f"endorsed by {e.endorsed_by}"
                if e.endorsed
                else "UNENDORSED, drafted automatically — treat as provisional"
            )
            lines.append(f"- **{e.term}** [{e.kind}; {mark}]: {e.definition.strip()}")
            stamp = f" (as of {e.as_of})" if e.as_of else ""
            if e.status:
                lines.append(f"    status{stamp}: {e.status}")
            if e.timeline:
                lines.append(f"    timeline{stamp}: {e.timeline}")
        return (
            "Glossary — this group's own vocabulary for instruments they build "
            "and phenomena they study. Use it to interpret the question. It is "
            "NOT channel evidence: never cite it. Any status or timeline is a "
            "snapshot taken on the date shown; if the excerpts below are more "
            "recent, trust the excerpts and say the glossary is behind:\n"
            + "\n".join(lines)
        )

    def query_expansion(self, entries: list[Entry], max_words: int = 12) -> str:
        """Extra search terms for a matched entry, most valuable first.

        The term and its aliases lead. A question that says "X-ray
        spectroscopy" needs the token ``XRD`` added to reach chunks that only
        ever write the acronym — and that token appears nowhere in the entry's
        prose. Drawing expansion from the definition alone dropped precisely
        the word the search was missing.
        """
        words: list[str] = []

        def add(text: str) -> None:
            for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-]{2,}", text):
                if tok.lower() not in {w.lower() for w in words}:
                    words.append(tok)

        for e in entries:
            add(e.term)
            for alias in e.aliases:
                add(alias)
        for e in entries:
            add(e.definition)
        return " ".join(words[:max_words])


# ------------------------------------------------------------------- HTML view

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1rem; font:16px/1.6 -apple-system,BlinkMacSystemFont,
  "Segoe UI",Roboto,sans-serif; background:#fff; color:#1a1a1a; }
.wrap { max-width: 46rem; margin: 0 auto; }
h1 { font-size:1.6rem; margin:0 0 .25rem; }
.sub { color:#666; margin:0 0 2rem; font-size:.9rem; }
.entry { border-top:1px solid #e5e5e5; padding:1.1rem 0; }
.term { font-size:1.1rem; font-weight:650; display:flex; align-items:center;
  gap:.6rem; flex-wrap:wrap; }
.def { margin:.4rem 0 .5rem; }
.badge { font-size:.7rem; font-weight:600; letter-spacing:.03em; padding:.15rem .5rem;
  border-radius:999px; text-transform:uppercase; }
.ok { background:#e7f5ec; color:#1a7f43; }
.prov { background:#fdf3e3; color:#96631a; }
.meta { font-size:.82rem; color:#666; }
.vol { margin:.45rem 0 .3rem; padding:.5rem .7rem; border-left:3px solid #d8b95e;
  background:#fbf7ec; border-radius:0 4px 4px 0; font-size:.9rem; }
.vol div { margin:.1rem 0; }
.vol .k { font-weight:600; }
.asof { color:#8a7028; font-size:.78rem; margin-top:.25rem; }
h2.kind { font-size:.8rem; text-transform:uppercase; letter-spacing:.08em;
  color:#888; margin:2.2rem 0 .2rem; font-weight:700; }
.alias { font-size:.82rem; color:#666; font-style:italic; }
.scope { font-size:.7rem; font-weight:600; padding:.15rem .5rem; border-radius:999px;
  background:#eef2f8; color:#4a5b74; letter-spacing:.02em; }
.scope.all { background:#f0ecf8; color:#5b4a74; }
code { background:#f2f2f2; padding:.1rem .3rem; border-radius:3px; font-size:.85em; }
@media (prefers-color-scheme: dark) {
  body { background:#16181c; color:#e6e6e6; }
  .sub,.meta,.alias { color:#9aa0a6; }
  .entry { border-color:#2c2f36; }
  .scope { background:#242a33; color:#9fb0c9; }
  .scope.all { background:#2b2635; color:#b5a2d4; }
  .ok { background:#14351f; color:#79d99b; }
  .prov { background:#3a2e14; color:#e0b552; }
  code { background:#24272d; }
  .vol { background:#2b2617; border-left-color:#8a6f22; }
  .asof { color:#c9a94f; }
  h2.kind { color:#7d848e; }
}
"""


def render_html(
    entries: list[Entry],
    title: str = "Glossary",
    channel_names: dict[str, str] | None = None,
) -> str:
    n_ok = sum(1 for e in entries if e.endorsed)
    names = channel_names or {}

    def block(e: Entry) -> str:
        badge = (
            '<span class="badge ok">endorsed</span>'
            if e.endorsed
            else '<span class="badge prov">unendorsed</span>'
        )
        if e.channels:
            shown = ", ".join("#" + names.get(c, c) for c in e.channels)
            badge += f'<span class="scope">{html.escape(shown)}</span>'
        else:
            badge += '<span class="scope all">all channels</span>'
        alias = (
            f'<div class="alias">also: {html.escape(", ".join(e.aliases))}</div>'
            if e.aliases
            else ""
        )
        vol = ""
        if e.status or e.timeline:
            rows = []
            if e.status:
                rows.append(
                    f'<div><span class="k">status:</span> {html.escape(e.status)}</div>'
                )
            if e.timeline:
                rows.append(
                    f'<div><span class="k">timeline:</span> {html.escape(e.timeline)}</div>'
                )
            stamp = (
                f'<div class="asof">snapshot as of {html.escape(e.as_of)} — '
                f"the channel may have moved on</div>"
                if e.as_of
                else ""
            )
            vol = f'<div class="vol">{"".join(rows)}{stamp}</div>'
        meta = []
        if e.endorsed_by:
            meta.append(f"endorsed by {html.escape(e.endorsed_by)}")
        if e.drafted:
            meta.append(f"drafted {html.escape(e.drafted)}")
        meta_html = f'<div class="meta">{" · ".join(meta)}</div>' if meta else ""
        return (
            f'<div class="entry"><div class="term">{html.escape(e.term)}{badge}</div>'
            f'{alias}<div class="def">{html.escape(e.definition)}</div>'
            f"{vol}{meta_html}</div>"
        )

    sections = []
    labels = {INSTRUMENT: "Instruments &amp; parts", PHENOMENON: "Scientific terms"}
    for kind in KINDS:
        group = sorted(
            (e for e in entries if e.kind == kind), key=lambda x: x.term.lower()
        )
        if not group:
            continue
        sections.append(f'<h2 class="kind">{labels[kind]} ({len(group)})</h2>')
        sections.extend(block(e) for e in group)

    other = sorted(
        (e for e in entries if e.kind not in KINDS), key=lambda x: x.term.lower()
    )
    if other:
        sections.append(f'<h2 class="kind">Other ({len(other)})</h2>')
        sections.extend(block(e) for e in other)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>{html.escape(title)}</h1>
<p class="sub">{len(entries)} terms · {n_ok} endorsed · {len(entries) - n_ok} awaiting review.
Generated from <code>glossary.md</code> — edit that file, not this page.</p>
{"".join(sections) or "<p>No terms yet.</p>"}
</div></body></html>
"""
