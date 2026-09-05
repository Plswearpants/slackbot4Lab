"""What counts as indexable channel content.

The corpus is what humans said to each other. Everything else is excluded:

* **Our own replies** — indexing them creates a self-citation loop, where a
  wrong answer becomes a citable source and the error compounds on every
  related question.
* **The @mention questions** — those are queries, not knowledge.
* **Other bots and app webhooks** (CI, Jira, alerting) — in an active channel
  these dominate by volume, and boilerplate matches everything while answering
  nothing.
* **Slack system messages** — joins, leaves, topic and purpose changes.
"""

from __future__ import annotations

import re
from collections.abc import Container, Mapping
from types import MappingProxyType
from typing import Any

# Slack message subtypes that carry no conversational content.
_SYSTEM_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
        "group_topic",
        "group_purpose",
        "group_name",
        "group_archive",
        "group_unarchive",
        "pinned_item",
        "unpinned_item",
        "bot_add",
        "bot_remove",
        "reminder_add",
        "tombstone",
    }
)

_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)>")
# Slack writes channel references as <#C123|name> or bare <#C123>.
_CHANNEL_RE = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]*))?>")


def is_indexable(event: dict[str, Any], bot_user_id: str | None = None) -> bool:
    """True if this message event should enter the corpus."""
    if event.get("subtype") in _SYSTEM_SUBTYPES:
        return False

    # Any bot-authored message: ours or a third party's.
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return False

    user = event.get("user")
    if not user:
        return False
    if bot_user_id and user == bot_user_id:
        return False

    text = (event.get("text") or "").strip()
    if not text:
        return False

    # Questions directed at us are queries, not content.
    if bot_user_id and bot_user_id in mentioned_users(text):
        return False

    return True


def mentioned_users(text: str) -> set[str]:
    return set(_MENTION_RE.findall(text))


def strip_mentions(text: str) -> str:
    """Remove every <@U123> token.

    Only appropriate where the identities genuinely do not matter. For a
    question, prefer :func:`name_mentions`: deleting the mention erases who was
    asked about, turning "what is @Markus working on" into "what is working on".
    """
    return _MENTION_RE.sub("", text).strip()


def name_mentions(
    text: str,
    names: Mapping[str, str],
    drop: Container[str] = frozenset(),
    channels: Mapping[str, str] = MappingProxyType({}),
) -> str:
    """Replace <@U123> with a display name, dropping ids in ``drop``.

    The bot's own mention is noise and belongs in ``drop``; every other person
    named in a question is part of what was asked. Substituting the name also
    lets retrieval work, since chunks are rendered with display names.
    """

    def swap(m: re.Match[str]) -> str:
        uid = m.group(1)
        if uid in drop:
            return ""
        return names.get(uid, uid)

    def swap_channel(m: re.Match[str]) -> str:
        # "<#CLXDY4AK1>" reached a question verbatim and matched nothing.
        # Slack sometimes carries the name, sometimes only the id.
        return "#" + (m.group(2) or channels.get(m.group(1), m.group(1)))

    text = _MENTION_RE.sub(swap, text)
    text = _CHANNEL_RE.sub(swap_channel, text)
    return " ".join(text.split())
