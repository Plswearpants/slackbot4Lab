"""Local-first completion, with the hosted model as the safety net."""
from __future__ import annotations

import pytest

from slackqa.answerer import FallbackCompleter


class Stub:
    def __init__(self, reply="", fail=None):
        self.reply, self.fail, self.calls = reply, fail, 0

    async def complete(self, system, user):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.reply

    async def check_credentials(self):
        if self.fail:
            raise self.fail


async def test_the_local_cluster_answers_when_it_can():
    local, hosted = Stub("local answer"), Stub("hosted answer")
    c = FallbackCompleter(local, hosted)
    assert await c.complete("s", "u") == "local answer"
    assert hosted.calls == 0 and c.last_used == "local"


async def test_an_unreachable_cluster_routes_to_openrouter():
    local = Stub(fail=ConnectionError("connection refused"))
    hosted = Stub("hosted answer")
    c = FallbackCompleter(local, hosted)
    assert await c.complete("s", "u") == "hosted answer"
    assert c.last_used == "openrouter"


async def test_an_empty_completion_counts_as_a_failure():
    """A 200 carrying nothing is indistinguishable, to the person asking, from
    the cluster being down."""
    c = FallbackCompleter(Stub("   "), Stub("hosted answer"))
    assert await c.complete("s", "u") == "hosted answer"


async def test_a_refusal_is_an_answer_and_is_never_second_guessed():
    """Refusing when the channel does not support an answer is the behaviour we
    want (D1). Retrying against another model until one is willing to speak
    would defeat the whole design."""
    from slackqa.answerer import NO_ANSWER

    local, hosted = Stub(NO_ANSWER), Stub("a confident invention")
    c = FallbackCompleter(local, hosted)
    assert await c.complete("s", "u") == NO_ANSWER
    assert hosted.calls == 0


async def test_repeated_failure_stops_paying_the_timeout_every_time():
    local = Stub(fail=TimeoutError("timed out"))
    hosted = Stub("hosted answer")
    c = FallbackCompleter(local, hosted, failures_before_pause=3, pause_seconds=300)
    for _ in range(5):
        await c.complete("s", "u")
    assert local.calls == 3, "the cluster should be skipped once it is clearly down"
    assert c.paused is True


async def test_the_cluster_is_retried_after_the_pause():
    local = Stub(fail=TimeoutError("x"))
    c = FallbackCompleter(local, Stub("hosted"), failures_before_pause=1,
                          pause_seconds=0.01)
    await c.complete("s", "u")
    assert c.paused
    import asyncio

    await asyncio.sleep(0.02)
    assert not c.paused
    local.fail = None
    local.reply = "back up"
    assert await c.complete("s", "u") == "back up"


async def test_recovery_resets_the_failure_count():
    local = Stub(fail=ConnectionError("x"))
    c = FallbackCompleter(local, Stub("hosted"), failures_before_pause=3)
    await c.complete("s", "u")
    await c.complete("s", "u")
    local.fail, local.reply = None, "recovered"
    await c.complete("s", "u")
    local.fail = ConnectionError("x")
    for _ in range(2):
        await c.complete("s", "u")
    assert not c.paused, "two fresh failures must not inherit the earlier count"


async def test_local_only_never_leaves_the_lab():
    """With no fallback configured, a dead cluster raises rather than quietly
    sending channel content to a hosted model."""
    from slackqa.answerer import CredentialsError

    c = FallbackCompleter(Stub(fail=ConnectionError("refused")), None)
    with pytest.raises(CredentialsError):
        await c.complete("s", "u")


async def test_startup_tolerates_a_cluster_that_is_down():
    """A cluster that is offline at startup must not stop the bot booting —
    that is what the fallback is for."""
    c = FallbackCompleter(Stub(fail=ConnectionError("refused")), Stub("ok"))
    await c.check_credentials()


async def test_local_only_startup_reports_a_cause_not_a_traceback():
    """An unreachable cluster under LOCAL_ONLY is a config problem the user has
    to read, not a stack trace — the CLI already renders CredentialsError."""
    from slackqa.answerer import CredentialsError

    c = FallbackCompleter(Stub(fail=ConnectionError("refused")), None)
    with pytest.raises(CredentialsError, match="LOCAL_ONLY"):
        await c.check_credentials()


# --------------------------------------- visibility when the VPN is the gate


async def test_fallbacks_are_counted_not_just_remembered():
    """The cluster sits behind a VPN, so falling back is a daily event. "What
    answered the last question" would hide a whole morning spent off-site."""
    local = Stub(fail=ConnectionError("no route"))
    c = FallbackCompleter(local, Stub("hosted"), failures_before_pause=99)
    await c.complete("s", "u")
    await c.complete("s", "u")
    local.fail, local.reply = None, "local answer"
    await c.complete("s", "u")
    assert (c.fallbacks, c.answers) == (2, 3)


async def test_reachability_is_recorded_for_the_dashboard():
    local = Stub(fail=ConnectionError("no route"))
    c = FallbackCompleter(local, Stub("hosted"))
    assert c.local_ok is None
    await c.complete("s", "u")
    assert c.local_ok is False
    local.fail, local.reply = None, "back"
    await c.complete("s", "u")
    assert c.local_ok is True


async def test_a_startup_check_records_reachability_without_failing():
    c = FallbackCompleter(Stub(fail=ConnectionError("no route")), Stub("ok"))
    await c.check_credentials()
    assert c.local_ok is False
