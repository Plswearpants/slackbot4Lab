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
    """Remove <@U123> tokens — used to turn a question into a clean query."""
    return _MENTION_RE.sub("", text).strip()
