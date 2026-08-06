"""Resolve Slack user IDs to display names, cached in SQLite.

Deliberately keyed on distinct users rather than messages. Resolving names
inline per message is how a Slack bot ends up making dozens of ``users.info``
calls to answer one question and then spends its life rate-limited.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

from slackqa.store import Store

logger = logging.getLogger(__name__)


def _display_name(info: object) -> str:
    """Best available human name from a users.info result.

    Accepts either a raw dict or a slack_sdk response object. Note that
    ``dict(response)`` raises TypeError on the real object — the response is not
    dict-convertible — so the payload must be reached via ``.data``.
    """
    data = getattr(info, "data", info)
    user = (data.get("user") if hasattr(data, "get") else None) or {}
    profile = user.get("profile") or {}
    return (
        profile.get("display_name")
        or profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
        or user.get("id")
        or "unknown"
    )


class NameResolver:
    def __init__(self, store: Store, client) -> None:
        self._store = store
        self._client = client

    async def resolve(self, user_ids: Iterable[str]) -> dict[str, str]:
        """Names for the given users, hitting the API only for cache misses."""
        out: dict[str, str] = {}
        missing: list[str] = []

        for uid in {u for u in user_ids if u}:
            cached = await self._store.get_user_name(uid)
            if cached:
                out[uid] = cached
            else:
                missing.append(uid)

        for uid in missing:
            try:
                name = _display_name(await self._client.users_info(user=uid))
            except Exception:
                # Fall back for this call, but deliberately do NOT cache it.
                # Caching a failure is indistinguishable from caching a real
                # name, and with no TTL one bad run poisons every chunk built
                # afterwards — which is exactly what happened when a TypeError
                # here silently wrote raw user IDs into the whole index.
                logger.warning("Could not resolve user %s", uid, exc_info=True)
                out[uid] = uid
                continue
            await self._store.cache_user_name(uid, name, time.time())
            out[uid] = name

        return out

    async def for_channel(self, channel_id: str) -> dict[str, str]:
        return await self.resolve(await self._store.distinct_users(channel_id))
