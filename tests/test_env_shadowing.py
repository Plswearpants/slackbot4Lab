"""An exported variable outranks .env — say so, rather than letting it puzzle."""
from __future__ import annotations

import logging

from slackqa.config import shadowed_settings, warn_about_shadowed_settings


def write(tmp_path, body):
    p = tmp_path / ".env"
    p.write_text(body)
    return p


def test_a_stale_export_is_reported(tmp_path, monkeypatch):
    env = write(tmp_path, "LOCAL_API_BASE=https://good/api\n")
    monkeypatch.setenv("LOCAL_API_BASE", "https://stale/:3000/api")
    assert shadowed_settings(env) == [
        ("LOCAL_API_BASE", "https://good/api", "https://stale/:3000/api")
    ]


def test_an_agreeing_export_is_not_a_problem(tmp_path, monkeypatch):
    """Sourcing .env and changing nothing is harmless; warning about it would
    train people to ignore the warning."""
    monkeypatch.setenv("MODEL", "x")
    env = write(tmp_path, "MODEL=x\n")
    assert shadowed_settings(env) == []


def test_a_variable_absent_from_the_file_is_not_shadowing(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    env = write(tmp_path, "MODEL=x\n")
    monkeypatch.setenv("SOMETHING_ELSE", "y")
    assert shadowed_settings(env) == []


def test_no_env_file_is_not_an_error(tmp_path):
    assert shadowed_settings(tmp_path / "nope") == []


def test_comments_and_blank_lines_are_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    env = write(tmp_path, "# LOCAL_MODEL=commented\n\nMODEL=real\n")
    monkeypatch.setenv("LOCAL_MODEL", "something")
    assert shadowed_settings(env) == []


def test_secret_values_are_never_logged(tmp_path, monkeypatch, caplog):
    """Naming the variable is enough to act on. Printing the credential is not
    worth the extra clarity, and logs outlive the key."""
    env = write(tmp_path, "OPENROUTER_API_KEY=sk-file-value\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-stale-exported-value")
    with caplog.at_level(logging.WARNING):
        warn_about_shadowed_settings(env)
    text = caplog.text
    assert "OPENROUTER_API_KEY" in text
    assert "sk-file-value" not in text
    assert "sk-stale-exported-value" not in text


def test_a_non_secret_shows_both_values(tmp_path, monkeypatch, caplog):
    """For a URL, the two values side by side are the whole diagnosis."""
    env = write(tmp_path, "LOCAL_API_BASE=https://good/api\n")
    monkeypatch.setenv("LOCAL_API_BASE", "https://stale/:3000/api")
    with caplog.at_level(logging.WARNING):
        warn_about_shadowed_settings(env)
    assert "https://good/api" in caplog.text
    assert "https://stale/:3000/api" in caplog.text


def test_the_warning_says_how_to_fix_it(tmp_path, monkeypatch, caplog):
    env = write(tmp_path, "LOCAL_API_BASE=a\nMODEL=b\n")
    monkeypatch.setenv("LOCAL_API_BASE", "x")
    monkeypatch.setenv("MODEL", "y")
    with caplog.at_level(logging.WARNING):
        warn_about_shadowed_settings(env)
    assert "unset LOCAL_API_BASE MODEL" in caplog.text


def test_silence_when_nothing_is_shadowed(tmp_path, caplog, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    env = write(tmp_path, "MODEL=x\n")
    with caplog.at_level(logging.WARNING):
        warn_about_shadowed_settings(env)
    assert caplog.text == ""
