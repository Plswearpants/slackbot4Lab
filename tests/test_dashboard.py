from __future__ import annotations

import time

import aiohttp
import pytest

from slackqa.config import Settings
from slackqa.dashboard import Runtime, StatusProbe, _ago, _duration, start
from slackqa.glossary import Entry, Glossary
from slackqa.store import Message

CH = "C0TEST"


class FakeSocket:
    """Matches the real aiohttp Socket Mode client, where is_connected is a
    coroutine. A synchronous fake here let a real bug through: the unawaited
    call returned a truthy coroutine and the indicator was green forever."""

    def __init__(self, connected: bool):
        self._connected = connected

    async def is_connected(self) -> bool:
        return self._connected


class FakeHandler:
    def __init__(self, connected: bool = True):
        self.client = FakeSocket(connected)


class FakeCompleter:
    def __init__(self, ok: bool = True, error: str = "rejected (401)"):
        self.ok = ok
        self.error = error
        self.checks = 0

    async def check_credentials(self) -> None:
        self.checks += 1
        if not self.ok:
            raise RuntimeError(self.error)


class FakeBot:
    def __init__(self, store, *, connected=True, key_ok=True, channels=(CH,)):
        self.store = store
        self.settings = Settings(
            slack_bot_token="x",
            slack_app_token="x",
            openrouter_api_key="x",
            channels=list(channels),
        )
        self.handler = FakeHandler(connected)
        self.completer = FakeCompleter(key_ok)
        self.runtime = Runtime()
        self.glossary = Glossary("/tmp/none.md", [Entry(term="a", definition="d")])

    async def channel_names(self):
        return {CH: "4probe"}


# ------------------------------------------------------------------ helpers


def test_ago_formats():
    assert _ago(None) == "never"
    assert _ago(5) == "5s ago"
    assert _ago(120) == "2m ago"
    assert _ago(7200).startswith("2h")
    assert _ago(90000).startswith("1d")


def test_duration_formats():
    assert _duration(90) == "1m"
    assert _duration(3700) == "1h 1m"
    assert _duration(90000).startswith("1d")


# --------------------------------------------------------------- indicators


async def test_listener_up_when_socket_connected(store):
    s = await StatusProbe(FakeBot(store, connected=True)).listener()
    assert s["ok"] is True
    assert s["state"] == "connected"


async def test_listener_flags_dropped_socket(store):
    # The process answering this request is alive by definition; the useful
    # signal is that its websocket has gone.
    s = await StatusProbe(FakeBot(store, connected=False)).listener()
    assert s["ok"] is False
    assert "socket down" in s["state"]


async def test_listener_reports_last_event_and_question(store):
    bot = FakeBot(store)
    bot.runtime.note_event()
    bot.runtime.note_question()
    s = await StatusProbe(bot).listener()
    assert s["last_event"].endswith("ago")
    assert s["last_question"].endswith("ago")


async def test_listener_never_seen_events(store):
    assert (await StatusProbe(FakeBot(store)).listener())["last_event"] == "never"


async def test_index_reports_counts_and_recency(store):
    now = time.time()
    await store.upsert_messages(
        [
            Message(CH, f"{now - 3600:.6f}", None, "U1", "older"),
            Message(CH, f"{now - 60:.6f}", None, "U1", "newer"),
        ]
    )
    s = await StatusProbe(FakeBot(store)).index()
    assert s["ok"] is True
    assert s["channels"][0]["messages"] == 2
    assert s["channels"][0]["channel"] == "4probe"
    assert s["newest_ago"].endswith("ago")


async def test_index_empty_is_not_ok(store):
    s = await StatusProbe(FakeBot(store)).index()
    assert s["ok"] is False
    assert s["newest"] == "—"


async def test_index_includes_glossary_counts(store):
    s = await StatusProbe(FakeBot(store)).index()
    assert s["glossary_terms"] == 1
    assert s["glossary_endorsed"] == 0


async def test_api_key_ok(store):
    s = await StatusProbe(FakeBot(store, key_ok=True)).api_key()
    assert s["ok"] is True
    assert s["detail"] == "accepted"


async def test_api_key_rejected_reports_reason(store):
    s = await StatusProbe(FakeBot(store, key_ok=False)).api_key()
    assert s["ok"] is False
    assert "rejected" in s["detail"]


async def test_api_key_result_is_cached(store):
    # A browser refreshing every 10s must not become a request per refresh.
    bot = FakeBot(store)
    probe = StatusProbe(bot)
    await probe.api_key()
    await probe.api_key()
    await probe.api_key()
    assert bot.completer.checks == 1


async def test_api_key_force_bypasses_cache(store):
    bot = FakeBot(store)
    probe = StatusProbe(bot)
    await probe.api_key()
    await probe.api_key(force=True)
    assert bot.completer.checks == 2


async def test_missing_completer_is_reported_not_raised(store):
    bot = FakeBot(store)
    bot.completer = None
    s = await StatusProbe(bot).api_key()
    assert s["ok"] is False


# --------------------------------------------------------------------- http


@pytest.fixture
async def base_url(store, unused_tcp_port):
    """A real server on a real port, so start() is exercised too."""
    runner = await start(FakeBot(store), "127.0.0.1", unused_tcp_port)
    yield f"http://127.0.0.1:{unused_tcp_port}"
    await runner.cleanup()


async def test_health_returns_all_three_indicators(base_url):
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base_url}/health") as resp:
            assert resp.status == 200
            data = await resp.json()
    assert set(data) >= {"listener", "index", "api_key"}
    assert data["listener"]["ok"] is True
    assert data["api_key"]["ok"] is True


async def test_page_is_served(base_url):
    async with aiohttp.ClientSession() as http:
        async with http.get(base_url + "/") as resp:
            assert resp.status == 200
            assert resp.content_type == "text/html"
            body = await resp.text()
    for label in ("Listener", "Index last updated", "API key"):
        assert label in body


async def test_page_renders_its_own_down_state(base_url):
    # The page must be able to show "unreachable" itself rather than relying on
    # the browser's connection error, so an already-open tab goes red.
    async with aiohttp.ClientSession() as http:
        async with http.get(base_url + "/") as resp:
            body = await resp.text()
    assert "unreachable" in body
    assert "/health" in body


async def test_busy_port_is_reported_not_fatal(store, unused_tcp_port):
    # A dashboard that cannot bind must never stop the bot answering questions.
    runner = await start(FakeBot(store), "127.0.0.1", unused_tcp_port)
    try:
        with pytest.raises(OSError):
            await start(FakeBot(store), "127.0.0.1", unused_tcp_port)
    finally:
        await runner.cleanup()


async def test_stale_key_is_detected(store, monkeypatch):
    # Replacing a dead key in .env does nothing until restart. The dashboard
    # must say so rather than re-probing the old key and reporting DOWN.
    bot = FakeBot(store, key_ok=False)
    probe = StatusProbe(bot)

    class FreshSettings:
        openrouter_api_key = "sk-or-v1-THE-NEW-ONE"

    monkeypatch.setattr("slackqa.config.Settings", lambda: FreshSettings())
    s = await probe.api_key()
    assert s["ok"] is False
    assert s["stale"] is True


async def test_matching_key_is_not_stale(store, monkeypatch):
    bot = FakeBot(store)
    probe = StatusProbe(bot)

    class Same:
        openrouter_api_key = bot.settings.openrouter_api_key

    monkeypatch.setattr("slackqa.config.Settings", lambda: Same())
    assert (await probe.api_key())["stale"] is False


async def test_indicator_can_actually_go_red(store):
    """Regression: is_connected is a coroutine, so an unawaited call was always
    truthy and this indicator could never report a dropped socket."""
    up = await StatusProbe(FakeBot(store, connected=True)).listener()
    down = await StatusProbe(FakeBot(store, connected=False)).listener()
    assert up["ok"] is True
    assert down["ok"] is False, "the listener card must be able to go red"
