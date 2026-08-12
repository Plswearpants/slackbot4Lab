"""Slack Socket Mode gateway.

Questions arrive as ``app_mention`` and are answered in a thread off the asking
message: the channel the question was asked in unambiguously defines the corpus,
and keeping answers visible means colleagues can see and correct them.

Workspace-level facts (bot user id, team URL) are fetched once at startup rather
than per message. So are display names, which live in SQLite. Resolving either
inline is how a bot ends up making dozens of API calls per question.
"""

from __future__ import annotations

import asyncio
import logging
import time

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from slackqa import dashboard, ingest
from slackqa.answerer import Answerer, CredentialsError, OpenRouterCompleter, Turn
from slackqa.config import Settings
from slackqa.embeddings import FastEmbedEmbedder
from slackqa.filters import strip_mentions
from slackqa.glossary import Glossary, SkipList, render_html
from slackqa.mining import mine, refresh_volatile
from slackqa.names import NameResolver
from slackqa.retrieval import Retriever
from slackqa.skills import Skill
from slackqa.store import Store

logger = logging.getLogger(__name__)


async def client_replies(client, channel_id: str, thread_ts: str) -> list[dict]:
    """All messages in a thread, oldest first."""
    resp = await client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
    return list(resp.get("messages") or [])


class SlackQA:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.embedder = FastEmbedEmbedder(settings.embed_model)
        self.app = AsyncApp(token=settings.slack_bot_token)
        self.names = NameResolver(store, self.app.client)
        self.retriever = Retriever(
            store,
            self.embedder,
            candidates=settings.candidates_per_retriever,
            rrf_k=settings.rrf_k,
            min_cosine=settings.relevance_threshold,
        )
        self.glossary = Glossary.load(settings.glossary_path)
        self.skill = Skill(settings.skill_path) if settings.skill_enabled else None
        self.skip = SkipList.load(settings.glossary_skip_path)
        self.completer: OpenRouterCompleter | None = None
        self._mining_task: asyncio.Task | None = None
        self._channel_names: dict[str, str] | None = None
        self.runtime = dashboard.Runtime()
        self.handler: AsyncSocketModeHandler | None = None
        self._dashboard: object | None = None
        self.bot_user_id: str | None = None
        self.team_url: str = ""
        self.answerer: Answerer | None = None
        # One lock per channel: a burst of mentions must not interleave
        # reindexing with retrieval on the same channel.
        self._locks: dict[str, asyncio.Lock] = {}
        self._register()

    def _lock(self, channel_id: str) -> asyncio.Lock:
        return self._locks.setdefault(channel_id, asyncio.Lock())

    # ------------------------------------------------------------------ setup

    async def identify(self) -> None:
        auth = await self.app.client.auth_test()
        self.bot_user_id = auth["user_id"]
        self.team_url = auth["url"].rstrip("/")
        completer = OpenRouterCompleter(
            self.settings.openrouter_api_key,
            self.settings.model,
            self.settings.max_answer_tokens,
            base_url=self.settings.openrouter_base_url,
            temperature=self.settings.temperature,
        )
        self.completer = completer
        self.answerer = Answerer(
            self.retriever,
            completer,
            team_url=self.team_url,
            top_k=self.settings.top_k,
            glossary=self.glossary if self.settings.glossary_enabled else None,
            skill=self.skill,
            store=self.store,
        )
        if self.skill:
            logger.info("Domain skill loaded: %s", self.settings.skill_path)
        logger.info("Authenticated as %s on %s", self.bot_user_id, self.team_url)
        await completer.check_credentials()
        logger.info("Model provider reachable, key accepted (model=%s)", self.settings.model)

    async def sync_channel(self, channel_id: str) -> None:
        """Bring one channel's index in line with Slack."""
        kw = {"gap_seconds": self.settings.chunk_gap_seconds}

        if not await self.store.is_backfilled(channel_id):
            logger.info("No index for %s — backfilling", channel_id)
            await ingest.ingest_range(
                self.store, self.app.client, channel_id, bot_user_id=self.bot_user_id
            )
            names = await self.names.for_channel(channel_id)
            await ingest.reindex_channel(
                self.store, self.embedder, channel_id, names=names, **kw
            )
            await self.store.mark_backfilled(channel_id, time.time())
            return

        names = await self.names.for_channel(channel_id)
        await ingest.catch_up(
            self.store,
            self.app.client,
            self.embedder,
            channel_id,
            bot_user_id=self.bot_user_id,
            names=names,
            **kw,
        )
        # Slack never replays events missed while we were down, so deletions
        # have to be found by diffing rather than waited for.
        await ingest.reconcile_deletions(
            self.store,
            self.app.client,
            self.embedder,
            channel_id,
            window_days=self.settings.reconcile_window_days,
            names=names,
            **kw,
        )

    async def sync_all(self) -> None:
        for channel_id in self.settings.channels:
            try:
                await self.sync_channel(channel_id)
            except Exception:
                logger.exception("Sync failed for channel=%s", channel_id)
        self.runtime.last_sync_at = time.time()

    # --------------------------------------------------------------- handlers

    def _register(self) -> None:
        self.app.event("app_mention")(self._on_mention)
        self.app.event("message")(self._on_message)

    async def _thread_turns(self, channel_id: str, thread_ts: str, event_ts: str) -> list[Turn]:
        """Prior turns of the thread we're replying in, oldest first.

        Includes our own earlier answers — that is the memory. This is separate
        from what gets indexed: the index excludes bot replies to avoid a
        self-citation loop, but within one thread our last answer is exactly
        what a follow-up like "no, that's wrong" refers to.
        """
        if thread_ts == event_ts:
            return []  # top-level mention: no prior turns
        try:
            resp = await client_replies(self.app.client, channel_id, thread_ts)
        except Exception:
            logger.warning("Could not read thread %s", thread_ts, exc_info=True)
            return []

        names = await self.names.for_channel(channel_id)
        turns: list[Turn] = []
        for m in resp:
            if str(m.get("ts")) == str(event_ts):
                continue  # the question being asked right now
            text = (m.get("text") or "").strip()
            if not text:
                continue
            uid = m.get("user") or ""
            is_bot = bool(m.get("bot_id")) or uid == self.bot_user_id
            speaker = "assistant" if is_bot else names.get(uid, uid)
            turns.append(Turn(speaker=speaker, text=strip_mentions(text), is_bot=is_bot))
        return turns[-self.settings.thread_turns :]

    async def _mining_loop(self) -> None:
        """Draft glossary entries for recurring jargon, periodically."""
        interval = self.settings.glossary_update_hours * 3600
        while True:
            await asyncio.sleep(interval)
            try:
                await self.run_mining()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Glossary mining pass failed")

    async def run_mining(self) -> list[str]:
        assert self.completer is not None
        added: list[str] = []
        for channel_id in self.settings.channels:
            added += await mine(
                self.store,
                self.glossary,
                self.completer,
                channel_id,
                skip=self.skip,
                max_new_terms=self.settings.glossary_max_new_terms,
                min_chunks=self.settings.glossary_min_conversations,
            )
            # Definitions are stable; status and timeline are not. Re-derive the
            # snapshots that have aged so the glossary never quietly contradicts
            # a fresher message in the channel.
            await refresh_volatile(
                self.store,
                self.glossary,
                self.completer,
                channel_id,
                max_age_days=self.settings.glossary_refresh_days,
                max_per_pass=self.settings.glossary_max_refresh,
            )
        await self.write_glossary_html()
        return added

    async def channel_names(self) -> dict[str, str]:
        """Channel id -> name, for display only. Ids stay canonical in the file."""
        if self._channel_names is None:
            names: dict[str, str] = {}
            for ch in self.settings.channels:
                try:
                    info = await self.app.client.conversations_info(channel=ch)
                    names[ch] = info["channel"].get("name") or ch
                except Exception:
                    names[ch] = ch
            self._channel_names = names
        return self._channel_names

    async def write_glossary_html(self) -> None:
        path = self.settings.glossary_html_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_html(self.glossary.entries, channel_names=await self.channel_names())
        )
        logger.info("Glossary HTML written to %s", path)

    async def _on_mention(self, event: dict, client) -> None:
        self.runtime.note_event()
        self.runtime.note_question()
        channel_id = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        question = strip_mentions(event.get("text") or "")

        if not question:
            return
        if channel_id not in self.settings.channels:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text="I'm not indexing this channel, so I can't answer here.",
            )
            return

        assert self.answerer is not None
        try:
            await client.reactions_add(
                channel=channel_id, timestamp=event["ts"], name="thinking_face"
            )
        except Exception:
            pass  # a missing reaction must never block the answer

        try:
            turns = await self._thread_turns(channel_id, thread_ts, event["ts"])
            async with self._lock(channel_id):
                answer = await self.answerer.answer(channel_id, question, thread=turns)
            await client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts, text=answer.text
            )
            await self.store.log_query(
                channel_id,
                event.get("user") or "",
                question,
                answer.chunk_ids,
                answer.refused,
                time.time(),
            )
        except Exception as e:
            logger.exception("Failed answering in channel=%s", channel_id)
            auth = isinstance(e, CredentialsError) or type(e).__name__ == "AuthenticationError"
            text = (
                "My model provider rejected the API key, so I can't answer until "
                "it's renewed. Nothing is wrong with the channel index."
                if auth
                else "Something went wrong answering that — check the logs."
            )
            await client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)
        finally:
            try:
                await client.reactions_remove(
                    channel=channel_id, timestamp=event["ts"], name="thinking_face"
                )
            except Exception:
                pass

    async def _on_message(self, event: dict) -> None:
        """Keep the index in step with new messages, edits and deletions."""
        self.runtime.note_event()
        channel_id = event.get("channel")
        if channel_id not in self.settings.channels:
            return

        subtype = event.get("subtype")
        kw = {"gap_seconds": self.settings.chunk_gap_seconds}

        try:
            async with self._lock(channel_id):
                names = await self.names.for_channel(channel_id)
                if subtype == "message_deleted":
                    await ingest.handle_delete(
                        self.store, self.embedder, channel_id, event, names=names, **kw
                    )
                elif subtype == "message_changed":
                    await ingest.handle_edit(
                        self.store,
                        self.embedder,
                        channel_id,
                        event,
                        bot_user_id=self.bot_user_id,
                        names=names,
                        **kw,
                    )
                elif subtype is None or subtype == "thread_broadcast":
                    user = event.get("user")
                    if user:
                        fresh = await self.names.resolve([user])
                        names = {**names, **fresh}
                    await ingest.handle_new_message(
                        self.store,
                        self.embedder,
                        channel_id,
                        event,
                        bot_user_id=self.bot_user_id,
                        names=names,
                        **kw,
                    )
        except Exception:
            logger.exception("Ingest failed for channel=%s", channel_id)

    # ------------------------------------------------------------------- run

    async def start(self) -> None:
        handler = AsyncSocketModeHandler(self.app, self.settings.slack_app_token)
        self.handler = handler
        if self.settings.dashboard_enabled:
            try:
                self._dashboard = await dashboard.start(
                    self, self.settings.dashboard_host, self.settings.dashboard_port
                )
            except OSError as e:
                # A busy port must not stop the bot answering questions.
                logger.warning(
                    "Status dashboard not started (port %d): %s",
                    self.settings.dashboard_port, e,
                )
        if self.settings.glossary_enabled:
            self._mining_task = asyncio.create_task(self._mining_loop())
            logger.info(
                "Glossary: %d term(s) loaded, mining every %.1fh",
                len(self.glossary.entries),
                self.settings.glossary_update_hours,
            )
        logger.info("slackqa listening on %d channel(s)", len(self.settings.channels))
        try:
            await handler.start_async()
        finally:
            if self._mining_task:
                self._mining_task.cancel()
            if self._dashboard is not None:
                await self._dashboard.cleanup()


async def build(settings: Settings) -> SlackQA:
    store = await Store.open(settings.db_path)
    bot = SlackQA(settings, store)
    try:
        await bot.identify()
    except BaseException:
        # aiosqlite runs each connection on its own non-daemon thread, so a
        # store left open after a failed startup keeps the process alive
        # forever — the error prints and the command never returns.
        await store.close()
        raise
    return bot
