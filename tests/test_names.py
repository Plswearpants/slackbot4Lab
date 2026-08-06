from __future__ import annotations

from slackqa.names import NameResolver

CH = "C0TEST"


class FakeSlackResponse:
    """Mimics slack_sdk's response object, including the trap.

    The real AsyncSlackResponse is NOT dict-convertible: ``dict(response)``
    raises TypeError. A fake that returned a plain dict let a real bug through
    once already, so this one refuses conversion the same way.
    """

    def __init__(self, data: dict):
        self.data = data

    def __iter__(self):
        return iter(self.data)

    def __getitem__(self, key):
        return self.data[key]


class CountingClient:
    def __init__(self, names: dict[str, str] | None = None, fail: set[str] | None = None):
        self._names = names or {}
        self._fail = fail or set()
        self.calls: list[str] = []

    async def users_info(self, user: str):
        self.calls.append(user)
        if user in self._fail:
            raise RuntimeError("user_not_found")
        return FakeSlackResponse(
            {"user": {"id": user, "profile": {"display_name": self._names[user]}}}
        )


async def test_resolves_and_caches(store):
    client = CountingClient({"U1": "alice", "U2": "bob"})
    r = NameResolver(store, client)

    assert await r.resolve(["U1", "U2"]) == {"U1": "alice", "U2": "bob"}
    assert sorted(client.calls) == ["U1", "U2"]

    # Second pass is served entirely from SQLite.
    assert await r.resolve(["U1", "U2"]) == {"U1": "alice", "U2": "bob"}
    assert sorted(client.calls) == ["U1", "U2"]


async def test_duplicate_ids_cost_one_call(store):
    client = CountingClient({"U1": "alice"})
    await NameResolver(store, client).resolve(["U1", "U1", "U1"])
    assert client.calls == ["U1"]


async def test_failure_falls_back_to_id_but_is_not_cached(store):
    # Caching a failure is indistinguishable from caching a real name, and with
    # no TTL one bad run poisons every chunk built afterwards. Retry instead.
    client = CountingClient({}, fail={"U9"})
    r = NameResolver(store, client)
    assert await r.resolve(["U9"]) == {"U9": "U9"}
    assert await store.get_user_name("U9") is None
    await r.resolve(["U9"])
    assert client.calls == ["U9", "U9"]  # retried rather than trusting garbage


async def test_real_name_fallback(store):
    class C:
        calls: list[str] = []

        async def users_info(self, user: str):
            return FakeSlackResponse(
                {"user": {"id": user, "profile": {"real_name": "Carol Danvers"}}}
            )

    assert await NameResolver(store, C()).resolve(["U3"]) == {"U3": "Carol Danvers"}


async def test_for_channel_uses_stored_messages(store):
    from slackqa.store import Message

    await store.upsert_messages(
        [
            Message(CH, "100.000000", None, "U1", "hi"),
            Message(CH, "200.000000", None, "U2", "hey"),
            Message(CH, "300.000000", None, "U1", "again"),
        ]
    )
    client = CountingClient({"U1": "alice", "U2": "bob"})
    names = await NameResolver(store, client).for_channel(CH)
    assert names == {"U1": "alice", "U2": "bob"}
    assert sorted(client.calls) == ["U1", "U2"]


async def test_empty_input_makes_no_calls(store):
    client = CountingClient()
    assert await NameResolver(store, client).resolve([]) == {}
    assert client.calls == []


def test_display_name_handles_response_object_not_just_dict():
    # dict(AsyncSlackResponse) raises TypeError; the payload lives on .data.
    from slackqa.names import _display_name

    payload = {"user": {"id": "U1", "profile": {"display_name": "Ken Wong"}}}
    assert _display_name(FakeSlackResponse(payload)) == "Ken Wong"
    assert _display_name(payload) == "Ken Wong"


def test_display_name_never_silently_returns_the_raw_id_for_a_good_response():
    from slackqa.names import _display_name

    resp = FakeSlackResponse({"user": {"id": "U1", "profile": {"display_name": "alice"}}})
    assert _display_name(resp) != "U1"
