from __future__ import annotations

import pytest

from slackqa.config import Settings

REQUIRED = {
    "SLACK_BOT_TOKEN": "xoxb-x",
    "SLACK_APP_TOKEN": "xapp-x",
    "OPENROUTER_API_KEY": "sk-or-x",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    # Isolate from the developer's real .env and exported variables.
    for key in list(REQUIRED) + ["CHANNELS", "MODEL", "DATA_DIR", "TOP_K"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def build(**env) -> Settings:
    import os

    os.environ.update({**REQUIRED, **env})
    return Settings()  # type: ignore[call-arg]


def test_channels_parses_comma_separated():
    # The real-world failure: pydantic-settings JSON-decodes complex types at
    # the source level, so a bare list[str] rejects "C1,C2" before validation.
    assert build(CHANNELS="C0123ABC,C0456DEF").channels == ["C0123ABC", "C0456DEF"]


def test_channels_tolerates_whitespace_and_blanks():
    assert build(CHANNELS=" C1 , C2 ,, ").channels == ["C1", "C2"]


def test_channels_single_value():
    assert build(CHANNELS="C0123ABC").channels == ["C0123ABC"]


def test_channels_empty_by_default():
    assert build().channels == []


def test_defaults_are_openrouter():
    s = build()
    assert s.model == "anthropic/claude-sonnet-5"
    assert s.openrouter_base_url == "https://openrouter.ai/api/v1"


def test_db_path_derives_from_data_dir():
    assert build(DATA_DIR="/tmp/xyz").db_path.as_posix() == "/tmp/xyz/slackqa.db"


def test_missing_required_key_raises():
    import os

    from pydantic import ValidationError

    for k in REQUIRED:
        os.environ.pop(k, None)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


async def test_build_closes_the_store_when_startup_fails(tmp_path, monkeypatch):
    """A failed startup must not leave the sqlite thread running.

    aiosqlite gives each connection a non-daemon thread; leaking one makes the
    CLI print its error and then hang instead of exiting.
    """
    import slackqa.app as app_mod
    from slackqa.answerer import CredentialsError

    closed: list[bool] = []

    class FakeStore:
        async def close(self):
            closed.append(True)

    async def fake_open(_path):
        return FakeStore()

    class FakeBot:
        def __init__(self, settings, store):
            pass

        async def identify(self):
            raise CredentialsError("key rejected")

    monkeypatch.setattr(app_mod.Store, "open", staticmethod(fake_open))
    monkeypatch.setattr(app_mod, "SlackQA", FakeBot)

    with pytest.raises(CredentialsError):
        await app_mod.build(build())
    assert closed == [True]
