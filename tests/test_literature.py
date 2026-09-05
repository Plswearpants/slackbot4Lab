from __future__ import annotations

from slackqa.literature import Paper, doi_from_url, extract

# Real shapes taken from #coolpapers.


def ids(text):
    return {r.identity for r in extract(text)}


# ------------------------------------------------------------------ detection


def test_plain_doi():
    assert "10.1103/PhysRevLett.133.176201" in ids("see 10.1103/PhysRevLett.133.176201")


def test_doi_trailing_punctuation_is_not_part_of_it():
    # A DOI at the end of a sentence must not swallow the full stop.
    assert "10.1126/science.adg8715" in ids("great result in 10.1126/science.adg8715.")


def test_arxiv_abs_and_pdf_links():
    assert ids("https://arxiv.org/abs/2412.02813") == {"arXiv:2412.02813"}
    assert ids("https://arxiv.org/pdf/2412.02813v2") == {"arXiv:2412.02813"}


def test_bare_arxiv_reference():
    assert "arXiv:2603.01226" in ids("worth reading, arXiv: 2603.01226")


def test_publisher_urls_yield_dois_without_a_network_call():
    cases = {
        "https://doi.org/10.1103/PhysRevB.108.094505": "10.1103/PhysRevB.108.094505",
        "https://journals.aps.org/prb/abstract/10.1103/PhysRevB.108.094505":
            "10.1103/PhysRevB.108.094505",
        "https://pubs.acs.org/doi/10.1021/acsnano.3c01234": "10.1021/acsnano.3c01234",
        "https://www.science.org/doi/10.1126/science.adg8715": "10.1126/science.adg8715",
        "https://www.nature.com/articles/s41586-023-06542-2":
            "10.1038/s41586-023-06542-2",
    }
    for url, doi in cases.items():
        assert doi_from_url(url) == doi, url


def test_slack_angle_bracket_links_are_unwrapped():
    text = "look at <https://arxiv.org/abs/2412.02813|this one>"
    assert "arXiv:2412.02813" in ids(text)


def test_same_paper_twice_in_one_message_yields_one_reference():
    text = "10.1126/science.adg8715 and again https://doi.org/10.1126/science.adg8715"
    assert len(extract(text)) == 1


def test_arxiv_link_does_not_also_register_as_a_bare_url():
    refs = extract("https://arxiv.org/abs/2412.02813")
    assert len(refs) == 1
    assert refs[0].arxiv_id == "2412.02813"


def test_non_paper_urls_are_kept_as_plain_references():
    # A link to a wiki or a vendor page is a reference we cannot resolve; it
    # should surface as unresolved rather than be silently dropped.
    refs = extract("https://example.com/some/page")
    assert len(refs) == 1 and refs[0].doi is None and refs[0].arxiv_id is None


def test_message_with_no_links():
    assert extract("the tip crashed again") == []


# --------------------------------------------------------------- zotero shape


def test_journal_article_maps_to_zotero():
    p = Paper(
        title="A paper", authors=[{"creatorType": "author", "firstName": "A",
                                   "lastName": "Author"}],
        date="2024-01-01", doi="10.1/x", url="https://doi.org/10.1/x",
        container="Phys. Rev. B", item_type="journalArticle",
    )
    z = p.to_zotero()
    assert z["itemType"] == "journalArticle"
    assert z["DOI"] == "10.1/x"
    assert z["publicationTitle"] == "Phys. Rev. B"


def test_preprint_records_its_arxiv_id():
    p = Paper(title="A preprint", arxiv_id="2412.02813", item_type="preprint",
              container="arXiv")
    z = p.to_zotero()
    assert z["itemType"] == "preprint"
    assert z["archiveID"] == "arXiv:2412.02813"
    assert "publicationTitle" not in z


def test_long_fields_are_truncated_not_rejected():
    z = Paper(title="x" * 900, abstract="y" * 9000).to_zotero()
    assert len(z["title"]) <= 500
    assert len(z["abstractNote"]) <= 5000


# ----------------------------------------------- real-world message messiness


def test_slack_escapes_ampersands():
    from slackqa.literature import normalise_url

    url = "https://example.org/a?x=1&amp;y=2"
    assert "&amp;" not in normalise_url(url)


def test_tracking_parameters_do_not_split_one_paper_into_two():
    # Observed: the same NAP page shared twice, once with a utm campaign tail.
    from slackqa.literature import normalise_url

    plain = "https://nap.nationalacademies.org/catalog/26594/frontiers"
    tagged = plain + "?utm_source=NASEM&utm_campaign=174dcd635f"
    assert normalise_url(tagged) == normalise_url(plain)


def test_slack_link_display_text_is_not_a_second_reference():
    # <url|display> where the display text is a truncated copy of the URL.
    text = "<https://example.org/paper/full-length-title|https://example.org/paper/full…>"
    assert len(extract(text)) == 1


def test_aps_pdf_url_resolves_to_the_same_doi_as_the_abstract():
    a = doi_from_url("https://journals.aps.org/prx/abstract/10.1103/PhysRevX.12.011012")
    b = doi_from_url("https://journals.aps.org/prx/pdf/10.1103/PhysRevX.12.011012")
    assert a == b == "10.1103/PhysRevX.12.011012"


def test_arxiv_feed_title_is_not_mistaken_for_the_paper_title():
    """The Atom feed opens with <title>ArXiv Query: ...</title> before the
    entry. Scoping to <entry> is what stops every paper being titled that."""
    import re

    xml = (
        "<feed><title>ArXiv Query: search_query=&amp;id_list=2208.05492</title>"
        "<entry><title>The Real Paper Title</title>"
        "<summary>An abstract.</summary><published>2022-08-10T00:00:00Z</published>"
        "<author><name>Jane Q Physicist</name></author></entry></feed>"
    )
    entry = re.search(r"<entry>(.*?)</entry>", xml, re.DOTALL).group(1)
    title = re.search(r"<title>(.*?)</title>", entry, re.DOTALL).group(1)
    assert title == "The Real Paper Title"


def test_publisher_view_suffixes_are_not_part_of_the_doi():
    from slackqa.literature import clean_doi

    # IOP serves .../article/<doi>/meta; leaving /meta on makes it unresolvable.
    assert clean_doi("10.1088/1361-648X/ae8497/meta") == "10.1088/1361-648X/ae8497"
    assert clean_doi("10.1002/adma.202301234/full") == "10.1002/adma.202301234"
    assert clean_doi("10.1103/PhysRevB.108.094505") == "10.1103/PhysRevB.108.094505"


def test_more_publisher_url_shapes():
    cases = {
        "https://iopscience.iop.org/article/10.1088/1361-648X/ae8497/meta":
            "10.1088/1361-648X/ae8497",
        "https://onlinelibrary.wiley.com/doi/10.1002/adma.202301234":
            "10.1002/adma.202301234",
        "https://link.springer.com/article/10.1007/s11467-023-1300-8":
            "10.1007/s11467-023-1300-8",
    }
    for url, doi in cases.items():
        assert doi_from_url(url) == doi, url


def test_attachment_mtime_is_milliseconds():
    """Zotero rejects a missing or zero mtime with 'File modification time not
    provided' — a message that names the field but not the unit."""
    import time

    from slackqa.zotero import Attachment

    att = Attachment("x.pdf", b"%PDF-1.4 test")
    now_ms = time.time() * 1000
    assert abs(att.mtime - now_ms) < 5000
    assert att.mtime > 1_000_000_000_000  # milliseconds, not seconds


def test_attachment_md5_is_of_the_content():
    import hashlib

    from slackqa.zotero import Attachment

    body = b"%PDF-1.4 hello"
    assert Attachment("x.pdf", body).md5 == hashlib.md5(body).hexdigest()


def test_publisher_pdf_urls_resolve_to_the_same_doi_as_the_landing_page():
    # Observed: the same ACS paper filed once by DOI, then counted again as
    # "unresolved" because /doi/pdf/ was not a recognised shape.
    abs_url = "https://pubs.acs.org/doi/10.1021/acsnano.1c05986"
    pdf_url = "https://pubs.acs.org/doi/pdf/10.1021/acsnano.1c05986?ref=article_openPDF"
    assert doi_from_url(abs_url) == doi_from_url(pdf_url) == "10.1021/acsnano.1c05986"


def test_query_string_is_not_part_of_the_doi():
    from slackqa.literature import clean_doi

    assert clean_doi("10.1021/acsnano.1c05986?ref=article_openPDF") == (
        "10.1021/acsnano.1c05986"
    )


# ----------------------------------------------------- reader tags from @mentions


def test_mentions_are_captured_with_the_reference():
    text = "square net superconductor <@USQCN4F9R> <@U8JQ4HFV3> https://arxiv.org/abs/2412.02813"
    refs = extract(text)
    assert len(refs) == 1
    assert refs[0].mentions == ["U8JQ4HFV3", "USQCN4F9R"]


def test_every_reference_in_a_message_inherits_its_mentions():
    # Someone posting two links and tagging a colleague means both papers.
    text = "<@U01513DKXRQ> these two: 10.1103/PhysRevB.108.094505 arXiv:2412.02813"
    refs = extract(text)
    assert len(refs) == 2
    assert all(r.mentions == ["U01513DKXRQ"] for r in refs)


def test_no_mentions_leaves_the_list_empty():
    assert extract("just a link https://arxiv.org/abs/2412.02813")[0].mentions == []


def test_reader_tag_uses_a_grouping_prefix():
    from slackqa.zotero import READER_TAG_PREFIX, reader_tag

    # Zotero's tag selector sorts alphabetically, so a shared prefix keeps
    # every reader tag together instead of scattered among subject tags.
    assert reader_tag("Markus") == "for:Markus"
    assert reader_tag("  Sarah  ").startswith(READER_TAG_PREFIX)


def test_mathml_is_stripped_from_titles():
    """Crossref embeds MathML in titles: a subscripted WP2 arrives as fifty
    tags around one character, which indexes as junk and reads as corruption."""
    from slackqa.literature import strip_markup

    raw = ("Observation of Weyl Nodes in Robust Type-II Weyl Semimetal "
           '<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML" '
           "display=\"inline\"><mml:mrow><mml:msub><mml:mrow><mml:mi>WP</mml:mi>"
           "</mml:mrow><mml:mrow><mml:mn>2</mml:mn></mml:mrow></mml:msub>"
           "</mml:mrow></mml:math>")
    out = strip_markup(raw)
    assert "mml:" not in out and "<" not in out
    # Adjacent tags carry no whitespace, so the subscript rejoins correctly:
    # the formula comes out as WP2, which is what it should read as.
    assert out == "Observation of Weyl Nodes in Robust Type-II Weyl Semimetal WP2"


def test_strip_markup_collapses_whitespace_and_entities():
    from slackqa.literature import strip_markup

    assert strip_markup("a  &amp;  b\n\nc") == "a & b c"


async def test_resolved_but_unfiled_papers_are_not_treated_as_done(store):
    """Resolving metadata records a row too. Treating any recorded reference as
    handled made the filing pass skip six hundred papers it had never filed."""
    await store.record_reference("10.1/x", "C1", "indexed", title="T")
    assert await store.seen_reference("10.1/x") is True
    assert await store.filed_reference("10.1/x") is False

    await store.record_reference("10.1/x", "C1", "added", title="T", zotero_key="ABC")
    assert await store.filed_reference("10.1/x") is True
