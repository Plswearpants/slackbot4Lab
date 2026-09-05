from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from slackqa.profiles import INSTRUMENT, PERSON, Entry, Profile, Profiles
from slackqa.profileweb import attach, render_index, render_profile


def sample(tmp_path):
    store = Profiles(tmp_path)
    store.save(Profile(
        name="tesla", kind=INSTRUMENT,
        abstract="A Joule-Thomson STM with ARPES.\n\nSecond paragraph.",
        systems="LaSbTe, NbSe2",
        source="https://lair.phas.ubc.ca/instruments/tesla/",
        timeline=[Entry("2026-08", "Recent month."), Entry("2019", "A whole year.")],
    ))
    store.save(Profile(name="Jisun", kind=PERSON, abstract="Runs the lab.",
                       timeline=[Entry("2026-08", "Active.")]))
    store.save(Profile(name="Seokhwan", kind=PERSON, abstract="Former member.",
                       timeline=[Entry("2021", "Left.")]))
    return store


def test_index_groups_and_flags(tmp_path):
    html = render_index(sample(tmp_path).all())
    assert "Instruments (1)" in html and "People (2)" in html
    assert "dormant" in html          # Seokhwan
    assert "unreviewed" in html       # nothing endorsed yet


def test_detail_distinguishes_month_from_year(tmp_path):
    """The rolling resolution is the point — a reader must see whether they are
    looking at a month of detail or a year of summary."""
    p = sample(tmp_path).load("tesla", INSTRUMENT)
    html = render_profile(p)
    assert ">month<" in html and ">year<" in html
    assert 'class="entry year"' in html


def test_detail_shows_systems_and_seed_provenance(tmp_path):
    html = render_profile(sample(tmp_path).load("tesla", INSTRUMENT))
    assert "LaSbTe" in html and "NbSe2" in html
    assert "seeded from" in html and "lair.phas.ubc.ca" in html


def test_paragraphs_survive(tmp_path):
    html = render_profile(sample(tmp_path).load("tesla", INSTRUMENT))
    assert html.count("<p>") >= 2


def test_content_is_escaped():
    html = render_profile(Profile(name="x", abstract="<script>alert(1)</script>"))
    assert "<script>" not in html and "&lt;script&gt;" in html


@pytest.fixture
async def base_url(tmp_path, unused_tcp_port):
    app = web.Application()
    attach(app, tmp_path)
    sample(tmp_path)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    yield f"http://127.0.0.1:{unused_tcp_port}"
    await runner.cleanup()


async def test_routes(base_url):
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base_url}/profiles") as r:
            assert r.status == 200
            assert "LAIR profiles" in await r.text()
        async with http.get(f"{base_url}/profiles/instrument/tesla") as r:
            assert r.status == 200
            assert "Joule-Thomson" in await r.text()
        async with http.get(f"{base_url}/profiles.json") as r:
            data = await r.json()
        async with http.get(f"{base_url}/profiles/person/nobody") as r:
            assert r.status == 404
    assert {d["name"] for d in data} == {"tesla", "Jisun", "Seokhwan"}


async def test_files_are_reread_so_edits_show_without_a_restart(tmp_path, base_url):
    store = Profiles(tmp_path)
    p = store.load("Jisun", PERSON)
    p.abstract = "Edited by hand."
    store.save(p)
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base_url}/profiles/person/jisun") as r:
            assert "Edited by hand." in await r.text()


# ----------------------------------------------------- endorse and edit


@pytest.fixture
async def app_url(tmp_path, unused_tcp_port, store):
    app = web.Application()
    attach(app, tmp_path, store)
    sample(tmp_path)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    yield f"http://127.0.0.1:{unused_tcp_port}", Profiles(tmp_path), store
    await runner.cleanup()


async def test_endorse_writes_the_name_and_date(app_url):
    url, profiles, _ = app_url
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{url}/profiles/person/jisun/endorse",
                             data={"actor": "Dong Chen"}) as r:
            assert r.status == 200  # redirected and followed
    p = profiles.load("Jisun", PERSON)
    assert p.endorsed is True
    assert p.endorsed_by.startswith("Dong Chen (")


async def test_endorsement_is_audited(app_url):
    url, _, store = app_url
    async with aiohttp.ClientSession() as http:
        await http.post(f"{url}/profiles/person/jisun/endorse",
                        data={"actor": "Dong Chen"})
    rows = await store.profile_history("Jisun")
    assert rows[0]["action"] == "endorse"
    assert rows[0]["actor"] == "Dong Chen"


async def test_endorsing_without_a_name_is_refused(app_url):
    url, profiles, _ = app_url
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{url}/profiles/person/jisun/endorse",
                             data={"actor": "  "}) as r:
            assert r.status == 400
    assert profiles.load("Jisun", PERSON).endorsed is False


async def test_edit_saves_and_records_what_changed(app_url):
    url, profiles, store = app_url
    async with aiohttp.ClientSession() as http:
        await http.post(f"{url}/profiles/person/jisun/edit",
                        data={"actor": "Jisun", "abstract": "I run the Omicron.",
                              "systems": ""})
    assert profiles.load("Jisun", PERSON).abstract == "I run the Omicron."
    rows = await store.profile_history("Jisun")
    assert rows[0]["action"] == "edit" and "abstract" in rows[0]["detail"]


async def test_an_edit_that_changes_nothing_is_not_recorded(app_url):
    url, profiles, store = app_url
    p = profiles.load("Jisun", PERSON)
    async with aiohttp.ClientSession() as http:
        await http.post(f"{url}/profiles/person/jisun/edit",
                        data={"actor": "X", "abstract": p.abstract,
                              "systems": p.systems})
    assert await store.profile_history("Jisun") == []


async def test_the_page_says_it_is_unauthenticated(app_url):
    url, _, _ = app_url
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{url}/profiles/person/jisun") as r:
            body = await r.text()
    assert "Unauthenticated demo" in body
    assert "Endorse" in body and "Edit" in body


# ------------------------------------------------- per-entry endorse / edit


async def test_endorse_a_single_entry(app_url):
    url, profiles, store = app_url
    async with aiohttp.ClientSession() as http:
        await http.post(f"{url}/profiles/instrument/tesla/entry/2019/endorse",
                        data={"actor": "Sarah"})
    p = profiles.load("tesla", INSTRUMENT)
    assert p.entry_for("2019").endorsed_by.startswith("Sarah")
    # The other entry, and the profile itself, are untouched.
    assert p.entry_for("2026-08").endorsed_by is None
    assert p.endorsed is False
    assert (await store.profile_history("tesla"))[0]["action"] == "entry-endorse"


async def test_edit_a_single_entry_records_who(app_url):
    url, profiles, store = app_url
    async with aiohttp.ClientSession() as http:
        await http.post(f"{url}/profiles/instrument/tesla/entry/2019/edit",
                        data={"actor": "Jisun", "text": "Actually commissioned in 2018."})
    e = profiles.load("tesla", INSTRUMENT).entry_for("2019")
    assert e.text == "Actually commissioned in 2018."
    assert e.edited_by.startswith("Jisun")
    rows = await store.profile_history("tesla")
    assert rows[0]["action"] == "entry-edit" and "2019" in rows[0]["detail"]


async def test_an_entry_cannot_be_emptied(app_url):
    url, profiles, _ = app_url
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{url}/profiles/instrument/tesla/entry/2019/edit",
                             data={"actor": "X", "text": "   "}) as r:
            assert r.status == 400
    assert profiles.load("tesla", INSTRUMENT).entry_for("2019").text


async def test_unknown_period_is_a_404(app_url):
    url, _, _ = app_url
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{url}/profiles/instrument/tesla/entry/1999/endorse",
                             data={"actor": "X"}) as r:
            assert r.status == 404


async def test_every_entry_offers_its_own_controls(app_url):
    url, _, _ = app_url
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{url}/profiles/instrument/tesla") as r:
            body = await r.text()
    assert body.count("Endorse this entry") == 2   # one per timeline entry
    assert "unreviewed" in body
