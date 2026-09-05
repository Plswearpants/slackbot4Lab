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
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from slackqa.profiles import Profile
from slackqa.retrieval import Hit, Retriever

logger = logging.getLogger(__name__)

MAX_PROFILES = 3

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

9. Profiles are background on a person or an instrument, assembled from this \
channel's own history. They tell you who or what the question is about; they \
are not excerpts and never become claims. Rule 1 still holds: anything you \
assert must be supported by an excerpt, and a profile is not one. Never cite a \
profile. Where several profiles are offered as CANDIDATES for one ambiguous \
name, at most one of them is meant — choose the one the excerpts support. If \
the excerpts do not distinguish them, say the name is ambiguous and ask which \
person is meant. Do not blend two candidates into one person, and do not guess.

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
    deep: bool = False


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


def profile_block(
    certain: Sequence[Profile] = (),
    ambiguous: Sequence[Sequence[Profile]] = (),
    *,
    recent: int = 4,
) -> str:
    """Render profiles for the prompt: what is known, and what is ambiguous.

    Candidate groups are labelled explicitly and kept to abstracts. Unlabelled,
    a model reads three profiles for one name as three relevant people and
    blends them into a composite who does not exist.
    """

    def one(p: Profile, *, full: bool) -> str:
        out = [f"### {p.name} ({p.kind})"]
        if p.abstract:
            out.append(p.abstract.strip())
        if full:
            if p.systems:
                out.append("Sample systems: " + ", ".join(p.systems))
            for e in p.timeline[:recent]:
                out.append(f"{e.period}: {e.text}")
        return "\n".join(out)

    if not certain and not ambiguous:
        return ""

    parts = [
        "Profiles — background only. Use them to understand who or what is "
        "meant. They are not evidence and must never be cited."
    ]
    parts += [one(p, full=True) for p in certain]
    for group in ambiguous:
        names = ", ".join(p.name for p in group)
        parts.append(
            f"CANDIDATES — the question names someone whose first name could "
            f"mean any of: {names}. At most one is meant. Decide from the "
            f"excerpts; if they do not settle it, ask which is meant."
        )
        parts += [one(p, full=False) for p in group]
    return "\n\n".join(parts) + "\n"


def build_user_prompt(
    question: str,
    hits: Sequence[Hit],
    team_url: str,
    channel_id: str,
    *,
    thread: Sequence[Turn] | None = None,
    glossary_block: str = "",
    roster: Sequence[str] = (),
    profiles_block: str = "",
) -> str:
    sections: list[str] = []

    if roster:
        sections.append(
            "People in this channel: " + ", ".join(sorted(roster)) + ".\n"
            "A name in the question refers to one of them."
        )

    if glossary_block:
        sections.append(glossary_block)

    if profiles_block:
        sections.append(profiles_block)

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
    expansion: str = "",
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
        gloss_terms = glossary.query_expansion(matched)
        if gloss_terms:
            parts.append(gloss_terms)
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
        store: object | None = None,
        profiles: object | None = None,
    ) -> None:
        self._retriever = retriever
        self._completer = completer
        self._team_url = team_url
        self._top_k = top_k
        # Duck-typed rather than imported, so the answerer stays testable
        # without a glossary and the module dependency stays one-way.
        self._glossary = glossary
        self._skill = skill
        self._store = store
        self._profiles = profiles

    def _search_query(
        self,
        question: str,
        thread: Sequence[Turn] | None,
        channel_id: str,
        expansion: str = "",
    ) -> str:
        return build_search_query(
            question,
            glossary=self._glossary,
            channel_id=channel_id,
            thread=thread,
            expansion=expansion,
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
        deep: bool = False,
        roster: Sequence[str] = (),
    ) -> Answer:
        from slackqa.expansion import expand, strip_trigger, wants_expansion

        deep = deep or wants_expansion(question)
        question = strip_trigger(question)

        expansion = ""
        if deep:
            expansion = await expand(
                question,
                self._completer,
                glossary=self._glossary,
                channel_id=channel_id,
                store=self._store,
            )

        gloss = self._glossary_block(question, channel_id)
        query = self._search_query(question, thread, channel_id, expansion)

        hits = await self._retriever.retrieve(channel_id, query, self._top_k)
        if not hits:
            # Nothing indexed matched at all — refuse without spending a call.
            return Answer(
                text=_refusal_text(), refused=True, chunk_ids=[], searches=1, deep=deep
            )

        reply = await self._ask(channel_id, question, hits, thread, gloss, roster)

        match = _SEARCH_RE.search(reply)
        if match:
            refined = match.group(1).strip()
            logger.info("Refining search: %r -> %r", question, refined)
            hits2 = await self._retriever.retrieve(channel_id, refined, self._top_k)
            if hits2:
                hits = hits2
            # Second and final pass; any further SEARCH line is ignored below.
            reply = await self._ask(channel_id, question, hits, thread, gloss, roster)
            reply = _SEARCH_RE.sub("", reply).strip()
            return _finalize(reply, hits, searches=2, deep=deep)

        return _finalize(reply, hits, searches=1, deep=deep)

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
        roster: Sequence[str] = (),
    ) -> str:
        user = build_user_prompt(
            question,
            hits,
            self._team_url,
            channel_id,
            thread=thread,
            glossary_block=glossary_block,
            roster=roster,
            profiles_block=self._profiles_block(question, hits, roster),
        )
        return (await self._completer.complete(self._system(), user)).strip()

    def _profiles_block(
        self, question: str, hits: Sequence[Hit], roster: Sequence[str]
    ) -> str:
        """Profiles for whoever the question names.

        The retrieved excerpts are passed as evidence, so a first name shared by
        several people usually resolves here — only what the channel itself
        cannot settle is handed to the model as a candidate list.
        """
        if self._profiles is None:
            return ""
        try:
            certain, ambiguous = self._profiles.candidates(
                question,
                evidence="\n".join(h.text for h in hits),
                roster=roster,
            )
        except Exception:
            logger.warning("Profile lookup failed; answering without it", exc_info=True)
            return ""
        # A question naming four people should not crowd out the evidence the
        # answer has to cite.
        return profile_block(certain[:MAX_PROFILES], ambiguous[:MAX_PROFILES])


def _refusal_text() -> str:
    return (
        "I couldn't find anything in this channel's history that answers that. "
        "It may predate what I've indexed, or have been discussed elsewhere."
    )


def _finalize(
    reply: str, hits: Sequence[Hit], searches: int, deep: bool = False
) -> Answer:
    ids = [h.chunk_id for h in hits]
    if not reply or reply.strip() == NO_ANSWER:
        return Answer(
            text=_refusal_text(), refused=True, chunk_ids=ids, searches=searches, deep=deep
        )
    return Answer(
        text=reply, refused=False, chunk_ids=ids, searches=searches, deep=deep
    )


class LocalCompleter:
    """Completions from the lab's own cluster, via its OpenAI-compatible API.

    Open WebUI exposes the same protocol as OpenRouter, so this differs only in
    what it points at and in having no vendor-specific key endpoint to check —
    a model listing serves that purpose.
    """

    name = "local"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout: float = 90.0,
    ) -> None:
        from openai import AsyncOpenAI

        self._base_url = str(base_url).rstrip("/")
        self._client = AsyncOpenAI(
            api_key=api_key or "unused", base_url=self._base_url, timeout=timeout,
            max_retries=0,  # the fallback is the retry
        )
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._api_key = api_key

    async def check_credentials(self) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        if resp.status_code in (401, 403):
            raise CredentialsError(
                f"The local cluster rejected the API key ({resp.status_code}). "
                "Check LOCAL_API_KEY in .env against the key in Open WebUI."
            )
        if resp.status_code >= 400:
            raise CredentialsError(
                f"Local cluster check failed ({resp.status_code}): {resp.text[:200]}"
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
        if not resp.choices:
            raise RuntimeError("local cluster returned no choices")
        return resp.choices[0].message.content or ""


class FallbackCompleter:
    """Try the local cluster; fall back to a hosted model when it cannot answer.

    A *failure* is the cluster being unreachable, timing out, erroring, or
    returning nothing. A refusal is not a failure — if the model correctly says
    the channel does not support an answer, that is the answer, and asking a
    second model until one is willing to speak would defeat the point.

    Once the cluster has failed repeatedly there is no sense paying its timeout
    on every question, so it is skipped for a cooling-off period and then tried
    again.
    """

    def __init__(
        self,
        primary: Completer,
        fallback: Completer | None,
        *,
        failures_before_pause: int = 3,
        pause_seconds: float = 120.0,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._limit = failures_before_pause
        self._pause = pause_seconds
        self._failures = 0
        self._skip_until = 0.0
        self.last_used = ""
        # Counted, not just remembered. With the cluster behind a VPN a
        # fallback is a daily event rather than an outage, and "what answered
        # the last question" hides a morning spent answering off-site.
        self.answers = 0
        self.fallbacks = 0
        self.local_ok: bool | None = None

    @property
    def paused(self) -> bool:
        return time.monotonic() < self._skip_until

    async def check_credentials(self) -> None:
        """Startup check. The local cluster failing is a warning, not fatal —
        the fallback is what makes that survivable."""
        try:
            await self._primary.check_credentials()
            self.local_ok = True
        except Exception as exc:
            self.local_ok = False
            if self._fallback is None:
                # LOCAL_ONLY: there is nowhere else to go, so this is a config
                # problem the user has to see stated, not a traceback.
                raise CredentialsError(
                    f"The local cluster is unreachable ({exc}) and LOCAL_ONLY is "
                    "set, so there is no fallback. Check LOCAL_API_BASE in .env "
                    "and that you are on the lab network, or unset LOCAL_ONLY to "
                    "allow OpenRouter."
                ) from exc
            logger.warning("Local cluster unavailable at startup: %s", exc)
        if self._fallback is not None:
            await self._fallback.check_credentials()

    async def complete(self, system: str, user: str) -> str:
        if not self.paused:
            try:
                text = await self._primary.complete(system, user)
                if text.strip():
                    self._failures = 0
                    self.last_used = "local"
                    self.local_ok = True
                    self.answers += 1
                    return text
                raise RuntimeError("empty completion")
            except Exception as exc:
                self._failures += 1
                if self._failures >= self._limit:
                    self._skip_until = time.monotonic() + self._pause
                    logger.warning(
                        "Local cluster failed %d times; using the hosted model "
                        "for %.0fs", self._failures, self._pause,
                    )
                self.local_ok = False
                logger.warning("Local completion failed (%s); falling back", exc)
                if self._fallback is None:
                    raise CredentialsError(
                        f"The local cluster could not answer ({exc}) and "
                        "LOCAL_ONLY is set, so there is no fallback. Unset "
                        "LOCAL_ONLY to allow OpenRouter."
                    ) from exc

        if self._fallback is None:
            raise CredentialsError(
                "The local cluster is unavailable and LOCAL_ONLY is set, so no "
                "answer can be produced. Unset LOCAL_ONLY to fall back to "
                "OpenRouter."
            )
        self.last_used = "openrouter"
        self.answers += 1
        self.fallbacks += 1
        return await self._fallback.complete(system, user)


class OpenRouterCompleter:
    name = "openrouter"

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
        self._env_path = Path(".env")
        self._env_mtime = self._env_stat()

    def _env_stat(self) -> float | None:
        try:
            return self._env_path.stat().st_mtime
        except OSError:
            return None

    def _reload_key_if_changed(self) -> bool:
        """Pick up a rotated API key without a restart.

        Settings are otherwise read once at startup, so replacing a dead key in
        .env did nothing until the process was bounced — a trap hit twice in
        four days. Only the credential group is reloaded: channels need
        re-subscription, the dashboard port is already bound, and changing the
        embedding model would invalidate every stored vector.
        """
        mtime = self._env_stat()
        if mtime is None or mtime == self._env_mtime:
            return False
        self._env_mtime = mtime

        from slackqa.config import Settings

        try:
            fresh = Settings()  # bypasses the settings cache
        except Exception:
            return False
        if fresh.openrouter_api_key == self._api_key and fresh.model == self._model:
            return False

        from openai import AsyncOpenAI

        self._api_key = fresh.openrouter_api_key
        self._model = fresh.model
        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            default_headers={
                "HTTP-Referer": "https://github.com/local/slackqa",
                "X-Title": "slackqa",
            },
        )
        logger.info("Reloaded model credentials from .env (model=%s)", self._model)
        return True

    async def check_credentials(self) -> None:
        self._reload_key_if_changed()
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
        self._reload_key_if_changed()
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
