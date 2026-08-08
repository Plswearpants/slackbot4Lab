"""Turn a question plus retrieved excerpts into a grounded, cited answer.

Two retrievals at most. The model may reformulate its query exactly once when
the first set of excerpts misses — enough to recover from vocabulary mismatch,
bounded so latency stays in the range Slack users tolerate and so a failure is
attributable to either retrieval or generation rather than to an open-ended
loop.

Refusal is a first-class outcome. Answering from the model's own general
knowledge when the channel never discussed something is the failure that
destroys trust fastest, because it is indistinguishable from a real answer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from slackqa.retrieval import Hit, Retriever

logger = logging.getLogger(__name__)

NO_ANSWER = "NO_ANSWER"


class CredentialsError(RuntimeError):
    """The model provider rejected our credentials. Not a per-question failure."""

_SEARCH_RE = re.compile(r"^\s*SEARCH:\s*(.+)$", re.MULTILINE)

SYSTEM_PROMPT = f"""\
You answer questions about a Slack channel using only the excerpts provided.

Rules, in order of importance:

1. Ground every claim in the excerpts. Never use outside knowledge about the \
company, its people, or its systems. If the excerpts don't say it, you don't \
know it.
2. If the excerpts do not contain enough to answer, reply with exactly \
{NO_ANSWER} and nothing else. This is a good outcome, not a failure — a wrong \
answer is far worse than no answer.
3. Cite sources inline as Slack links in the form <PERMALINK|date>, using the \
permalink given with each excerpt. Put a citation immediately after each claim \
it supports.
4. Be concise. Slack, not an essay. Lead with the answer.
5. Quote people by name when it matters who said something. Note explicitly \
when the channel disagreed or never resolved a question.
6. Prefer recent excerpts when they conflict with older ones, and say so.
7. "Conversation so far" is the Slack thread you are replying in, including \
your own earlier answers. Use it to resolve what "it", "that" or "they" refer \
to, and to avoid repeating yourself. It is context, not evidence — never cite \
it as a source, and if someone corrects you there, accept the correction \
rather than defending the earlier answer.
8. Glossary definitions describe workspace vocabulary. Use them to interpret \
the question. They are not channel evidence, so do not cite them. If a \
definition is marked UNENDORSED, say which term you leaned on and that its \
definition is unconfirmed.

If the excerpts look like they missed the point of the question and a different \
search would plainly do better, reply with exactly one line:

SEARCH: <better search query>

Use that only when you have not already been given a second set of excerpts.\
"""


class Completer(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class Turn:
    """One message in the thread the question was asked in."""

    speaker: str
    text: str
    is_bot: bool = False


# Words that signal the question leans on something said earlier.
_ANAPHORA = re.compile(
    r"(?<!\w)(it|its|that|this|those|these|they|them|he|she|him|her|instead|"
    r"why not|no,|wrong|actually|but)(?!\w)",
    re.IGNORECASE,
)


def needs_thread_context(question: str, content_word_floor: int = 5) -> bool:
    """True when the question can't stand alone as a search query.

    "no, it does not look right, because Markus is already there" retrieves
    nothing useful on its own — the subject lives in the previous turn. Short
    or anaphoric questions get the thread's earlier questions folded into the
    search; self-contained ones are left alone so their terms aren't diluted.
    """
    from slackqa.retrieval import fts_query

    expr = fts_query(question)
    content_words = len(expr.split(" OR ")) if expr else 0
    return content_words < content_word_floor or bool(_ANAPHORA.search(question))


@dataclass
class Answer:
    text: str
    refused: bool = False
    chunk_ids: list[int] = field(default_factory=list)
    searches: int = 1


def permalink(team_url: str, channel_id: str, ts: str) -> str:
    """Build a Slack permalink without spending an API call on chat.getPermalink."""
    return f"{team_url.rstrip('/')}/archives/{channel_id}/p{ts.replace('.', '')}"


def _fmt_range(start: float, end: float) -> str:
    fmt = "%Y-%m-%d"
    a = datetime.fromtimestamp(start, tz=UTC).strftime(fmt)
    b = datetime.fromtimestamp(end, tz=UTC).strftime(fmt)
    return a if a == b else f"{a} to {b}"


def render_thread(turns: Sequence[Turn], max_chars: int = 4000) -> str:
    """Recent thread turns, oldest last-kept first, trimmed from the front."""
    lines = [f"{'you' if t.is_bot else t.speaker}: {t.text.strip()}" for t in turns]
    out: list[str] = []
    total = 0
    for line in reversed(lines):  # keep the most recent turns when trimming
        if total + len(line) > max_chars:
            break
        out.append(line)
        total += len(line)
    return "\n".join(reversed(out))


def build_user_prompt(
    question: str,
    hits: Sequence[Hit],
    team_url: str,
    channel_id: str,
    *,
    thread: Sequence[Turn] | None = None,
    glossary_block: str = "",
) -> str:
    sections: list[str] = []

    if glossary_block:
        sections.append(glossary_block)

    if thread:
        sections.append("Conversation so far (this thread):\n" + render_thread(thread))

    blocks = []
    for i, h in enumerate(hits, start=1):
        link = permalink(team_url, channel_id, h.anchor_ts)
        when = _fmt_range(h.chunk["start_ts"], h.chunk["end_ts"])
        kind = "thread" if h.chunk["kind"] == "thread" else "conversation"
        blocks.append(
            f"--- Excerpt {i} ({kind}, {when})\nPERMALINK: {link}\n{h.text}"
        )
    sections.append("Excerpts from this channel:\n\n" + "\n\n".join(blocks))
    sections.append(f"Question: {question}")

    return "\n\n".join(sections)


def build_search_query(
    question: str,
    *,
    glossary: object | None = None,
    channel_id: str | None = None,
    thread: Sequence[Turn] | None = None,
) -> str:
    """What to actually search for, which is not always the question.

    A follow-up borrows its subject from earlier in the thread, so short or
    anaphoric questions fold in the thread's earlier human turns. Matched
    glossary entries contribute their term, aliases and definition words, which
    is how a question saying "X-ray spectroscopy" reaches chunks that only ever
    write ``XRD``. Self-contained questions are searched verbatim — padding them
    would dilute their own terms.

    Module-level and shared with the eval harness on purpose: a retrieval eval
    that rebuilt this logic separately would measure a pipeline production does
    not run.
    """
    parts = [question]
    if thread and needs_thread_context(question):
        prior = [t.text for t in thread if not t.is_bot][-3:]
        if prior:
            parts.extend(prior)
            logger.info("Follow-up detected; searching with thread context")
    if glossary is not None:
        matched = glossary.detect(question, channel_id)
        expansion = glossary.query_expansion(matched)
        if expansion:
            parts.append(expansion)
    return " ".join(parts)


class Answerer:
    def __init__(
        self,
        retriever: Retriever,
        completer: Completer,
        *,
        team_url: str,
        top_k: int = 8,
        glossary: object | None = None,
        skill: object | None = None,
    ) -> None:
        self._retriever = retriever
        self._completer = completer
        self._team_url = team_url
        self._top_k = top_k
        # Duck-typed rather than imported, so the answerer stays testable
        # without a glossary and the module dependency stays one-way.
        self._glossary = glossary
        self._skill = skill

    def _search_query(
        self, question: str, thread: Sequence[Turn] | None, channel_id: str
    ) -> str:
        return build_search_query(
            question,
            glossary=self._glossary,
            channel_id=channel_id,
            thread=thread,
        )

    def _glossary_block(self, question: str, channel_id: str) -> str:
        if self._glossary is None:
            return ""
        return self._glossary.prompt_block(
            self._glossary.detect(question, channel_id)
        )

    async def answer(
        self,
        channel_id: str,
        question: str,
        *,
        thread: Sequence[Turn] | None = None,
    ) -> Answer:
        gloss = self._glossary_block(question, channel_id)
        query = self._search_query(question, thread, channel_id)

        hits = await self._retriever.retrieve(channel_id, query, self._top_k)
        if not hits:
            # Nothing indexed matched at all — refuse without spending a call.
            return Answer(text=_refusal_text(), refused=True, chunk_ids=[], searches=1)

        reply = await self._ask(channel_id, question, hits, thread, gloss)

        match = _SEARCH_RE.search(reply)
        if match:
            refined = match.group(1).strip()
            logger.info("Refining search: %r -> %r", question, refined)
            hits2 = await self._retriever.retrieve(channel_id, refined, self._top_k)
            if hits2:
                hits = hits2
            # Second and final pass; any further SEARCH line is ignored below.
            reply = await self._ask(channel_id, question, hits, thread, gloss)
            reply = _SEARCH_RE.sub("", reply).strip()
            return _finalize(reply, hits, searches=2)

        return _finalize(reply, hits, searches=1)

    def _system(self) -> str:
        """Base rules plus the domain skill, re-read if it changed on disk."""
        body = getattr(self._skill, "body", "") if self._skill is not None else ""
        if not body:
            return SYSTEM_PROMPT
        return f"{SYSTEM_PROMPT}\n\n--- Domain guidance for this workspace ---\n\n{body}"

    async def _ask(
        self,
        channel_id: str,
        question: str,
        hits: Sequence[Hit],
        thread: Sequence[Turn] | None = None,
        glossary_block: str = "",
    ) -> str:
        user = build_user_prompt(
            question,
            hits,
            self._team_url,
            channel_id,
            thread=thread,
            glossary_block=glossary_block,
        )
        return (await self._completer.complete(self._system(), user)).strip()


def _refusal_text() -> str:
    return (
        "I couldn't find anything in this channel's history that answers that. "
        "It may predate what I've indexed, or have been discussed elsewhere."
    )


def _finalize(reply: str, hits: Sequence[Hit], searches: int) -> Answer:
    ids = [h.chunk_id for h in hits]
    if not reply or reply.strip() == NO_ANSWER:
        return Answer(text=_refusal_text(), refused=True, chunk_ids=ids, searches=searches)
    return Answer(text=reply, refused=False, chunk_ids=ids, searches=searches)


class OpenRouterCompleter:
    """Completions via OpenRouter's OpenAI-compatible endpoint.

    OpenRouter takes the system prompt as the first message rather than as a
    separate parameter, which is the one shape difference from Anthropic's own
    API. The model itself is unchanged — ``anthropic/claude-sonnet-5`` is the
    same Sonnet 5, just reached through a different front door.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 1024,
        base_url: str = "https://openrouter.ai/api/v1",
        temperature: float = 0.0,
    ) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            # Optional attribution headers; they show the app name in
            # OpenRouter's dashboard and cost nothing.
            default_headers={
                "HTTP-Referer": "https://github.com/local/slackqa",
                "X-Title": "slackqa",
            },
        )
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._api_key = api_key
        self._base_url = base_url

    async def check_credentials(self) -> None:
        """Fail loudly at startup if the key is dead.

        Without this the first symptom is a 401 raised per question, twenty
        hours later, surfacing in Slack as a generic "something went wrong" —
        one Slack round trip burned per question, and nothing pointing at the
        actual cause.
        """
        import httpx

        url = f"{str(self._base_url).rstrip('/')}/key"
        async with httpx.AsyncClient(timeout=20) as http:
            resp = await http.get(url, headers={"Authorization": f"Bearer {self._api_key}"})
        if resp.status_code == 401:
            raise CredentialsError(
                "OpenRouter rejected the API key (401). Generate a new one at "
                "https://openrouter.ai/keys and update OPENROUTER_API_KEY in .env."
            )
        if resp.status_code >= 400:
            raise CredentialsError(
                f"OpenRouter key check failed ({resp.status_code}): {resp.text[:200]}"
            )

    async def complete(self, system: str, user: str) -> str:
        resp = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        # OpenRouter can return a 200 with no choices when an upstream provider
        # fails; treat that as empty rather than raising an IndexError, so the
        # caller degrades to a refusal instead of a stack trace in Slack.
        if not resp.choices:
            logger.warning("OpenRouter returned no choices for model=%s", self._model)
            return ""
        return resp.choices[0].message.content or ""
