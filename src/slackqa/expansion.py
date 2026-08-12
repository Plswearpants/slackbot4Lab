"""Opt-in query expansion for questions whose words don't match the channel's.

Dense retrieval already absorbs a lot of vagueness — "that blue sticky stuff"
finds the right conversation unaided. What it loses is the *specific* evidence:
asked "did we ever figure out what that gunk was", the bot answered correctly
that nothing was ever confirmed, but never surfaced the XRD attempt that is the
strongest thing anyone did about it. The refinement round did not rescue it,
because a model satisfied with plausible excerpts has no way to know what it was
not shown.

So expansion is a lever the asker pulls, not a tax on every question. Writing
``deep`` in front of a question spends one model call to translate vague wording
into the channel's own vocabulary before retrieval runs.

Opt-in also keeps the default path deterministic, which is what lets
``slackqa eval`` stay offline, free, and runnable on every change. Expansions
are cached by question text, so a repeated question costs nothing.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Leading word that turns expansion on. A bare word rather than a slash command
# because Slack intercepts "/…" before it ever reaches the bot.
_TRIGGER_RE = re.compile(r"^\s*(deep|dig|search hard(?:er)?)\b[:,]?\s*", re.IGNORECASE)

_EXPAND_PROMPT = """\
Rewrite a question so it retrieves well against a research group's Slack history.

The asker uses loose, everyday wording; the channel uses the group's own names,
part numbers and technique acronyms. Your job is to bridge that gap.

Return ONLY search terms, space-separated, on one line — no explanation, no
punctuation, no quotes. Aim for 8-16 terms. Include:

- the group's own name for the thing, if one of the known terms below fits
- the technique acronyms someone would have written when reporting on it
  (XRD, XPS, EDX, ARPES, LEED, STM, AFM, dI/dV)
- concrete physical words the discussion would have used
- keep any exact identifiers from the question (part numbers, materials, names)

Known vocabulary in this channel:
{vocabulary}

Question: {question}
"""


def wants_expansion(question: str) -> bool:
    return bool(_TRIGGER_RE.match(question))


def strip_trigger(question: str) -> str:
    """The question as asked, minus the trigger word."""
    return _TRIGGER_RE.sub("", question, count=1).strip()


def _vocabulary(glossary, channel_id: str | None, limit: int = 40) -> str:
    """Channel vocabulary offered to the rewriter as candidate targets.

    Giving the model the glossary is what lets it map "gunk" onto a term the
    channel actually uses. Without it the rewrite is guesswork about a lab it
    has never seen.
    """
    if glossary is None:
        return "(none recorded)"
    names: list[str] = []
    for e in glossary.for_channel(channel_id):
        names.append(e.term)
        names.extend(e.aliases)
    if not names:
        return "(none recorded)"
    return ", ".join(names[:limit])


async def expand(
    question: str,
    completer,
    *,
    glossary=None,
    channel_id: str | None = None,
    store=None,
) -> str:
    """Terms to add to the search, or "" if expansion produced nothing useful."""
    if store is not None:
        cached = await store.get_expansion(question)
        if cached is not None:
            logger.info("Using cached expansion for %r", question[:50])
            return cached

    prompt = _EXPAND_PROMPT.format(
        vocabulary=_vocabulary(glossary, channel_id), question=question
    )
    try:
        raw = await completer.complete("", prompt)
    except Exception:
        logger.exception("Query expansion failed; falling back to the question as asked")
        return ""

    # One line of bare terms. Models add prose and bullets regardless of
    # instruction, so take the longest line and strip decoration rather than
    # trusting the format.
    lines = [ln.strip(" -*•\t") for ln in raw.splitlines() if ln.strip()]
    terms = max(lines, key=len) if lines else ""
    terms = re.sub(r"[^\w\s.\-/]", " ", terms)
    terms = " ".join(dict.fromkeys(terms.split()))[:400]

    if store is not None and terms:
        await store.put_expansion(question, terms)
    logger.info("Expanded %r -> %r", question[:40], terms[:80])
    return terms
