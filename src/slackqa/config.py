"""Settings, loaded from environment / .env.

Deliberately exposed via ``get_settings()`` rather than a module-level instance:
instantiating at import time makes every module unimportable without a populated
.env, which breaks tests and turns a missing variable into an import traceback.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Slack
    slack_bot_token: str
    slack_app_token: str

    # Channels to index, comma-separated channel IDs (e.g. "C0123ABC,C0456DEF").
    #
    # NoDecode is required: pydantic-settings JSON-decodes complex field types
    # at the source level, before any validator runs, so a plain list[str] makes
    # "C0123ABC,C0456DEF" a parse error rather than something the validator
    # below ever sees.
    channels: Annotated[list[str], NoDecode] = []

    # LLM — routed via OpenRouter's OpenAI-compatible API
    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model: str = "anthropic/claude-sonnet-5"

    # Local cluster, tried first when configured. Open WebUI speaks the
    # OpenAI-compatible protocol, so the base URL is its /api path.
    local_api_base: str = ""
    local_api_key: str = ""
    local_model: str = ""
    # Generation on local hardware is slower than a hosted frontier model, but
    # an unbounded wait is worse than a fallback: the question is sitting in
    # Slack while nobody knows anything is wrong.
    local_timeout: float = 90.0
    # Refuse to fall back to OpenRouter. Turns "usually local" into a hard
    # guarantee that channel content never leaves the lab, at the cost of no
    # answers when the cluster is down.
    local_only: bool = False

    @property
    def local_enabled(self) -> bool:
        return bool(self.local_api_base and self.local_model)
    max_answer_tokens: int = 2048
    # Deterministic by default: the same question on an unchanged index should
    # give the same answer. Retrieval is already deterministic, so sampling was
    # the only source of run-to-run variation — and it was large enough to flip
    # a full evidence list into a refusal.
    temperature: float = 0.0

    # Storage
    data_dir: Path = Path("./data")

    # Chunking: gap between consecutive messages that starts a new window chunk
    chunk_gap_seconds: int = 600

    # Retrieval
    top_k: int = 8
    candidates_per_retriever: int = 30
    rrf_k: int = 60
    # Minimum fused score for a chunk to count as usable evidence. Below this
    # for every candidate, the bot refuses rather than answering ungrounded.
    relevance_threshold: float = 0.02

    # Embeddings (fastembed / ONNX, runs locally)
    embed_model: str = "BAAI/bge-small-en-v1.5"

    # Thread memory: how many prior turns of the current thread to carry
    thread_turns: int = 12

    # Domain skill injected into the answering prompt. Tracked in the repo
    # rather than data/, because it is authored guidance, not derived state.
    skill_enabled: bool = True
    skill_path: Path = Path("skills/answering/SKILL.md")

    evals_path: Path = Path("evals/retrieval.yaml")

    # Entity profiles. Markdown so agents edit the files directly and humans
    # can read a diff; the dashboard is a view over the same files.
    profiles_dir: Path = Path("profiles")
    profile_active_months: int = 6

    # Zotero. Writes land in a library the whole lab shares, so the write path
    # stays off until switched on deliberately.
    zotero_api_key: str = ""
    zotero_group_id: str = ""
    zotero_write_enabled: bool = False
    # Only these channels are scanned for papers — a paper-sharing channel is a
    # different thing from a channel the bot answers questions about.
    literature_channels: Annotated[list[str], NoDecode] = []
    literature_max_items: int = 10
    # Unpaywall requires a contact address on every request; it has no API key.
    unpaywall_email: str = ""

    # Status dashboard. Localhost by default: the page exposes channel names and
    # message counts, which are not secret but are not for the network either.
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765

    # Glossary
    glossary_enabled: bool = True
    glossary_update_hours: float = 24.0
    glossary_max_new_terms: int = 5
    glossary_min_conversations: int = 3
    # How old a status/timeline snapshot may get before it is re-derived.
    glossary_refresh_days: int = 7
    glossary_max_refresh: int = 5

    # Startup reconcile: how far back to diff stored ts against Slack to catch
    # deletions that happened while the process was down.
    reconcile_window_days: int = 30

    @field_validator("channels", "literature_channels", mode="before")
    @classmethod
    def _split_channels(cls, v: object) -> object:
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "slackqa.db"

    @property
    def glossary_path(self) -> Path:
        return self.data_dir / "glossary.md"

    @property
    def glossary_html_path(self) -> Path:
        return self.data_dir / "glossary.html"

    @property
    def glossary_skip_path(self) -> Path:
        return self.data_dir / "glossary-skip.txt"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


# Settings whose value would identify a credential if logged.
_SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD")


def shadowed_settings(env_path: Path | str = ".env") -> list[tuple[str, str, str]]:
    """Settings where an exported environment variable disagrees with .env.

    Environment variables outrank the file, so a stale export silently wins and
    editing .env does nothing. Worse, it defeats the API-key hot reload: a fresh
    key in the file is read and then overridden by the old exported one, which
    looks exactly like a key that was never updated.

    Sourcing .env into a shell is the usual cause — it copies every value into
    the environment, where the copy then outranks the original.

    Returns ``(name, file_value, env_value)`` for each disagreement.
    """
    from dotenv import dotenv_values

    path = Path(env_path)
    if not path.exists():
        return []
    out: list[tuple[str, str, str]] = []
    for name, file_value in dotenv_values(path).items():
        env_value = os.environ.get(name)
        if file_value is not None and env_value is not None and env_value != file_value:
            out.append((name, file_value, env_value))
    return out


def warn_about_shadowed_settings(env_path: Path | str = ".env") -> None:
    """Log a warning naming anything the environment is overriding."""
    shadowed = shadowed_settings(env_path)
    if not shadowed:
        return
    logger.warning(
        "%d setting(s) in your shell override %s. The environment wins, so "
        "editing the file will not change them:",
        len(shadowed),
        env_path,
    )
    for name, file_value, env_value in shadowed:
        if any(h in name.upper() for h in _SECRET_HINTS):
            # Naming the variable is enough to act on; printing a credential
            # into the log is not worth the extra clarity.
            logger.warning("  %s differs from the file (value not shown)", name)
        else:
            logger.warning("  %s=%s   (file says %s)", name, env_value, file_value)
    logger.warning(
        "  Fix with: unset %s   — or start from a new shell.",
        " ".join(n for n, _, _ in shadowed),
    )
