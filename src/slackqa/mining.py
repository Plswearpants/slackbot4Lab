"""Find this channel's own vocabulary and write it down properly.

The target is knowledge that exists nowhere else: the dimensions and wiring of
*this* group's breakout box, what *this* group means by a load lock. A generic
expansion ("STM means scanning tunneling microscope") is explicitly not wanted —
the model already knows that, and recording it only adds prompt noise.

Three stages, in increasing cost order:

1. **Candidates** — cheap and local. Frequent multi-word phrases plus acronyms,
   required to recur across several *separate* conversations. Most channel
   vocabulary is a lowercase noun phrase ("breakout box", "load lock"), which a
   pure acronym regex cannot see.
2. **Triage** — one batched model call classifies the whole shortlist as
   instrument, phenomenon, or neither. Rejections are written to a skip list so
   the same terms are never paid for twice.
3. **Definition** — one call per surviving term, demanding concrete specifics
   drawn from the excerpts, plus status and timeline when the channel says so.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

from slackqa.glossary import (
    INSTRUMENT,
    KINDS,
    Entry,
    Glossary,
    SkipList,
    normalize_term,
)
from slackqa.store import Store

logger = logging.getLogger(__name__)

_ACRONYM_RE = re.compile(
    r"(?<!\w)("
    r"[A-Z]{2,6}[0-9]{0,3}(?:-[A-Z0-9]{1,4})?"
    r"|[A-Z][0-9]{2,3}"
    r"|[0-9]+-[a-zA-Z]{3,}"
    r"|[a-zA-Z]{3,}-[a-zA-Z]{3,}"
    r")(?!\w)"
)

_WORD_RE = re.compile(r"[a-z][a-z0-9]{1,}")

# Function words that must not start or end a phrase, and cannot form one alone.
_GLUE = frozenset(
    """
    a an and are as at be been but by can did do does for from get got had has
    have how i if in into is it its just me my no not of off on once only or
    our out over own same she so some than that the their them then there these
    they this those to too under until up us very was we were what when where
    which while who why will with would you your all also am any because before
    both each few more most other same such too here now new next last one two
    three first second like make made take took give gave go going went come
    came see saw look looked think thought know knew want wanted need needed
    """.split()
)

# Chat/logistics vocabulary that recurs everywhere and defines nothing.
_CHATTER = frozenset(
    """
    meeting meetings today tomorrow yesterday morning afternoon evening week
    weeks month months year years time times day days hour hours minute minutes
    thanks thank please sorry sure okay yeah yep nope maybe question questions
    email emails message messages slack channel thread link links file files
    folder folders group people team everyone anyone someone guys
    """.split()
)

_TRIAGE_PROMPT = """\
Below are candidate terms pulled from one Slack channel, each with sample usage.

This channel belongs to a research group. Classify each term into exactly one of:

- instrument — a physical part, component, apparatus or piece of equipment that
  this group builds, buys, wires, or operates. Examples of the right shape:
  a custom adapter box, a vacuum chamber, a specific manipulator.
- phenomenon — a scientific effect, measurement, material property or technique
  that this group specifically studies or cares about.
- reject — anything else: funding bodies, currencies, vendor names, places,
  people, software, generic English, meeting logistics, or any term whose
  meaning is fully generic and not specific to this group's work.

Reject generously. A term only belongs if knowing this group's particular
meaning would help answer questions about their work. If a term's meaning is
just its dictionary meaning, reject it.

Return one line per term, exactly:

TERM :: instrument|phenomenon|reject

Candidates:
{candidates}
"""

_DEFINE_PROMPT = """\
Write a glossary entry for "{term}" as this research group uses it, based only
on the excerpts below.

The entry must capture what makes this group's usage specific. Be concrete:
dimensions, part numbers, connector types and counts, what connects to what,
which side is which, materials, who is building it. A generic textbook
definition is a failure — if the excerpts only support a generic definition,
reply with exactly: UNCLEAR

Return exactly these fields, each on its own line, omitting any you cannot
support from the excerpts:

DEFINITION: one or two sentences, densely specific.
STATUS: the current state in at most 15 words (e.g. "on build at the electronic
shop", "installed and pumping down", "blocked on Cryovac sign-off"). Report the
latest state only — not a history. Omit entirely if the excerpts never say.
TIMELINE: at most 15 words, the nearest upcoming or most recent milestone with
its date (e.g. "expected complete 2026-08-13"). Not a chronology. Omit entirely
if the excerpts never say.
ALIASES: other names or spellings used for it in the excerpts, comma-separated.
Omit if there are none.

Use the most recent excerpts for STATUS and TIMELINE when they disagree.

Excerpts:
{excerpts}
"""

_REFRESH_PROMPT = """\
Below are the most recent excerpts mentioning "{term}", which is described as:

{definition}

Report only its current state, from these excerpts.

STATUS: the current state, or omit the line if the excerpts do not say.
TIMELINE: dates or milestones, or omit the line if the excerpts do not say.

If nothing here updates the state, reply with exactly: NOCHANGE

Excerpts:
{excerpts}
"""


@dataclass(frozen=True)
class Candidate:
    term: str
    occurrences: int
    chunks: int


def _phrases(text: str, max_len: int = 3) -> list[str]:
    """Lowercase n-grams that could plausibly name something."""
    out: list[str] = []
    for sentence in re.split(r"[.!?\n]", text.lower()):
        words = _WORD_RE.findall(sentence)
        for n in range(2, max_len + 1):
            for i in range(len(words) - n + 1):
                gram = words[i : i + n]
                if gram[0] in _GLUE or gram[-1] in _GLUE:
                    continue
                if any(w in _CHATTER for w in gram):
                    continue
                if all(w in _GLUE for w in gram):
                    continue
                out.append(" ".join(gram))
    return out


def find_candidates(
    texts_by_chunk: dict[int, str],
    *,
    min_chunks: int = 3,
    limit: int = 40,
) -> list[Candidate]:
    """Terms recurring across at least ``min_chunks`` distinct conversations.

    Repetition inside one conversation is not evidence of shared vocabulary —
    a single thread naturally repeats its own topic.
    """
    per_chunk: dict[str, set[int]] = defaultdict(set)
    totals: Counter[str] = Counter()

    for chunk_id, text in texts_by_chunk.items():
        seen: set[str] = set()
        for match in _ACRONYM_RE.finditer(text):
            term = match.group(1)
            if len(term) >= 3:
                seen.add(term)
                totals[term] += 1
        for phrase in _phrases(text):
            seen.add(phrase)
            totals[phrase] += 1
        for term in seen:
            per_chunk[term].add(chunk_id)

    out = [
        Candidate(term=t, occurrences=totals[t], chunks=len(ids))
        for t, ids in per_chunk.items()
        if len(ids) >= min_chunks
    ]
    out.sort(key=lambda c: (-c.chunks, -c.occurrences, c.term.lower()))
    return out[:limit]


def parse_triage(reply: str) -> dict[str, str]:
    """Parse "TERM :: kind" lines into {term: kind}."""
    out: dict[str, str] = {}
    for line in reply.splitlines():
        if "::" not in line:
            continue
        term, _, kind = line.partition("::")
        term, kind = term.strip().lstrip("-*").strip(), kind.strip().lower()
        if term and kind in (*KINDS, "reject"):
            out[term] = kind
    return out


_NON_ALIASES = frozenset(
    {"none", "none confirmed", "n/a", "na", "unknown", "no aliases", "-", "same"}
)


def clean_aliases(raw: str, term: str = "") -> list[str]:
    """Split an alias list without being fooled by commas inside parentheses.

    Models like to write "4-probe JT (cryostat, JT stage)", and a naive split on
    commas turns that into the fragments "4-probe JT (cryostat" and "JT stage)".
    Parenthetical asides carry no matching value, so they are removed first.
    """
    out: list[str] = []
    for part in re.split(r",(?![^(]*\))", raw):
        alias = re.sub(r"\([^)]*\)?", "", part).strip(" \t.;:'\"")
        if not alias or len(alias) > 40 or "(" in alias or ")" in alias:
            continue
        if alias.lower() in _GLUE or alias.lower() in _CHATTER:
            continue
        # Models answer "none confirmed" rather than omitting the field, and
        # sometimes echo the term itself as its own alias.
        low = alias.lower()
        if low in _NON_ALIASES or low.startswith(("none", "no ", "n/a", "same as")):
            continue
        if '"' in alias or "'" in alias:
            continue  # prose non-answers arrive with stray quotes
        if term and normalize_term(alias) == normalize_term(term):
            continue
        if alias.lower() not in {a.lower() for a in out}:
            out.append(alias)
    return out


def parse_fields(reply: str) -> dict[str, str]:
    """Parse the labelled DEFINITION/STATUS/TIMELINE/ALIASES block."""
    fields: dict[str, str] = {}
    current: str | None = None
    for line in reply.splitlines():
        # Colon optional: models sometimes emit a bare "TIMELINE" to mean "no
        # timeline". Treated as a continuation, that label lands inside the
        # previous field's text.
        m = re.match(r"^\s*(DEFINITION|STATUS|TIMELINE|ALIASES)\s*:?\s*(.*)$", line)
        if m:
            current = m.group(1).lower()
            fields[current] = m.group(2).strip()
        elif current and line.strip():
            fields[current] = (fields[current] + " " + line.strip()).strip()
    return {k: v for k, v in fields.items() if v}


def _excerpts_for(texts: dict[int, str], term: str, limit: int, newest_first=False) -> str:
    pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
    items = sorted(texts.items(), reverse=newest_first)
    picked = [t[:900] for _, t in items if pattern.search(t)]
    return "\n\n---\n\n".join(picked[:limit])


async def mine(
    store: Store,
    glossary: Glossary,
    completer,
    channel_id: str,
    *,
    skip: SkipList | None = None,
    max_new_terms: int = 5,
    min_chunks: int = 3,
    excerpts_per_term: int = 6,
    shortlist: int = 40,
) -> list[str]:
    """Draft entries for undefined channel-specific terms. Returns terms added."""
    ids, _ = await store.embeddings_for_channel(channel_id)
    chunks = await store.chunks_by_id(ids)
    texts = {cid: c["text"] for cid, c in chunks.items()}
    if not texts:
        return []

    candidates = [
        c
        for c in find_candidates(texts, min_chunks=min_chunks, limit=shortlist)
        if not glossary.has(c.term, channel_id) and (skip is None or c.term not in skip)
    ]
    if not candidates:
        logger.info("Glossary mining: no new candidates")
        return []

    listing = "\n".join(
        f"- {c.term} (in {c.chunks} conversations): "
        f"{_excerpts_for(texts, c.term, 1)[:180]}"
        for c in candidates
    )
    try:
        verdicts = parse_triage(
            await completer.complete("", _TRIAGE_PROMPT.format(candidates=listing))
        )
    except Exception:
        logger.exception("Glossary triage failed")
        return []

    rejected = [t for t, k in verdicts.items() if k == "reject"]
    # Anything the model declined to classify is also out; leaving it unrecorded
    # means paying to triage it again on every future pass.
    unjudged = [c.term for c in candidates if c.term not in verdicts]
    if skip is not None:
        skip.add(*rejected, *unjudged)
        skip.save()
    logger.info(
        "Glossary triage: %d candidates -> %d in scope, %d rejected",
        len(candidates),
        len(verdicts) - len(rejected),
        len(rejected) + len(unjudged),
    )

    keep = [c for c in candidates if verdicts.get(c.term) in KINDS]
    added: list[str] = []
    for cand in keep:
        if len(added) >= max_new_terms:
            break
        # An entry added earlier in this same pass may have claimed this term as
        # an alias; the pre-loop filter cannot know that yet.
        if glossary.has(cand.term, channel_id):
            logger.info("Glossary: %r now covered by an alias, skipping", cand.term)
            continue
        excerpts = _excerpts_for(texts, cand.term, excerpts_per_term, newest_first=True)
        if not excerpts:
            continue
        try:
            reply = await completer.complete(
                "", _DEFINE_PROMPT.format(term=cand.term, excerpts=excerpts)
            )
        except Exception:
            logger.exception("Glossary definition failed for term=%s", cand.term)
            continue
        if reply.strip().upper().startswith("UNCLEAR"):
            if skip is not None:
                skip.add(cand.term)
            continue
        fields = parse_fields(reply)
        if not fields.get("definition"):
            continue
        entry = Entry(
            term=cand.term,
            definition=fields["definition"],
            kind=verdicts.get(cand.term, INSTRUMENT),
            status=fields.get("status"),
            timeline=fields.get("timeline"),
            as_of=date.today().isoformat()
            if fields.get("status") or fields.get("timeline")
            else None,
            aliases=clean_aliases(fields.get("aliases", ""), cand.term),
            # Scoped to where it was mined: the same word can name different
            # hardware in a different channel. Delete this line by hand to make
            # an entry apply everywhere.
            channels=[channel_id],
            drafted=f"agent ({date.today().isoformat()}), "
            f"seen in {cand.chunks} conversations",
        )
        if not glossary.add(entry):
            logger.info("Glossary: %r already covered by an existing entry", cand.term)
            continue
        added.append(cand.term)

    if skip is not None:
        skip.save()
    if added:
        glossary.save()
        logger.info("Glossary added %d term(s): %s", len(added), ", ".join(added))
    return added


async def refresh_volatile(
    store: Store,
    glossary: Glossary,
    completer,
    channel_id: str,
    *,
    max_age_days: int = 7,
    max_per_pass: int = 5,
    excerpts_per_term: int = 5,
) -> list[str]:
    """Re-derive status and timeline for entries whose snapshot has aged.

    Endorsed entries are left alone — a person signed off on that text, and
    silently rewriting it would make the endorsement meaningless.
    """
    stale = [
        e
        for e in glossary.for_channel(channel_id)
        if not e.endorsed and e.stale(max_age_days)
    ][:max_per_pass]
    if not stale:
        return []

    ids, _ = await store.embeddings_for_channel(channel_id)
    chunks = await store.chunks_by_id(ids)
    texts = {cid: c["text"] for cid, c in chunks.items()}

    updated: list[str] = []
    for entry in stale:
        excerpts = _excerpts_for(texts, entry.term, excerpts_per_term, newest_first=True)
        if not excerpts:
            continue
        try:
            reply = await completer.complete(
                "",
                _REFRESH_PROMPT.format(
                    term=entry.term, definition=entry.definition, excerpts=excerpts
                ),
            )
        except Exception:
            logger.exception("Glossary refresh failed for term=%s", entry.term)
            continue
        if reply.strip().upper().startswith("NOCHANGE"):
            entry.as_of = date.today().isoformat()  # re-checked, still current
            continue
        fields = parse_fields(reply)
        if not fields:
            continue
        entry.status = fields.get("status", entry.status)
        entry.timeline = fields.get("timeline", entry.timeline)
        entry.as_of = date.today().isoformat()
        updated.append(entry.term)

    glossary.save()
    if updated:
        logger.info("Glossary refreshed: %s", ", ".join(updated))
    return updated
