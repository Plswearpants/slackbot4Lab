"""Turn papers shared in Slack into properly-catalogued Zotero items.

Three tiers, in the order they are attempted, matching what is actually legal
and reliable rather than what is technically possible:

1. **Metadata, always.** Crossref answers for essentially any DOI and arXiv for
   any preprint, both free and without credentials. Every paper therefore gets a
   correctly-typed item even when its PDF is unreachable.
2. **PDF where it is freely available.** arXiv directly, or Unpaywall's index of
   legal open-access copies for a DOI.
3. **Otherwise a link and a flag.** The item is created with a URL attachment and
   tagged ``needs-pdf`` so a person can fetch it with the Zotero Connector,
   which runs in their authenticated browser and is the right tool for that job.

Deliberately no automated fetching through institutional subscriptions.
Publishers prohibit systematic downloading and enforce it against the
institution's whole IP range, so the cost of being caught falls on everyone at
UBC rather than on this bot.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

CROSSREF = "https://api.crossref.org/works"
UNPAYWALL = "https://api.unpaywall.org/v2"
ARXIV_API = "http://export.arxiv.org/api/query"

# Trailing punctuation is part of the sentence, not the identifier.
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]*[A-Za-z0-9])")
_ARXIV_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", re.IGNORECASE
)
_ARXIV_BARE_RE = re.compile(r"\barXiv:\s*([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>|)\]]+")
# Slack renders links as <url|display text>, and the display text is often a
# truncated copy of the URL. Matching the raw message would capture both and
# treat one paper as two.
_SLACK_LINK_RE = re.compile(r"<(https?://[^>|]+)(?:\|[^>]*)?>")
# Campaign parameters differ between two shares of the same page.
_TRACKING_RE = re.compile(r"[?&](utm_[^=]+|fbclid|gclid|mc_cid|mc_eid)=[^&]*", re.I)
_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)>")


def normalise_url(url: str) -> str:
    """Strip what differs between two shares of the same page."""
    url = html.unescape(url).strip().rstrip(">|,.\u2026")
    url = _TRACKING_RE.sub("", url)
    url = url.split("#")[0]
    if url.endswith("?"):
        url = url[:-1]
    return url

# Publisher landing pages we can turn into a DOI without fetching anything.
_DOI_FROM_URL = [
    re.compile(r"doi\.org/(10\.\d{4,9}/[^\s?#]+)", re.I),
    re.compile(r"journals\.aps\.org/[^/]+/(?:abstract|pdf)/(10\.\d{4,9}/[^\s?#]+)", re.I),
    re.compile(r"nature\.com/articles/([a-z0-9\-.]+)", re.I),
    re.compile(r"pubs\.acs\.org/doi/(?:abs/|full/|pdf/|epdf/)?(10\.\d{4,9}/[^\s?#]+)", re.I),
    re.compile(r"science\.org/doi/(?:abs/|full/|pdf/|epdf/)?(10\.\d{4,9}/[^\s?#]+)", re.I),
    re.compile(r"iopscience\.iop\.org/article/(10\.\d{4,9}/[^\s?#]+)", re.I),
    re.compile(r"onlinelibrary\.wiley\.com/doi/(?:abs/|full/)?(10\.\d{4,9}/[^\s?#]+)", re.I),
    re.compile(r"link\.springer\.com/article/(10\.\d{4,9}/[^\s?#]+)", re.I),
]

# Publishers append a view suffix to the DOI in their URLs. Left on, it makes
# the identifier unresolvable: 10.1088/1361-648X/ae8497/meta is not a DOI.
_DOI_SUFFIXES = ("/meta", "/full", "/pdf", "/abstract", "/html", "/epdf", "/citations")


def strip_markup(text: str) -> str:
    """Remove the markup registries embed in titles and abstracts.

    Crossref returns MathML inside titles — a subscripted formula arrives as
    fifty tags around one character. Indexed raw it costs tokens, feeds junk
    terms to BM25, and looks like corruption if the model quotes it.
    """
    text = re.sub(r"<[^>]+>", "", text or "")
    return " ".join(html.unescape(text).split())


def clean_doi(doi: str) -> str:
    doi = doi.split("?")[0].split("#")[0]
    doi = doi.strip().rstrip(".,;)>\u2026")
    lowered = doi.lower()
    for suffix in _DOI_SUFFIXES:
        if lowered.endswith(suffix):
            return doi[: -len(suffix)]
    return doi


@dataclass
class Reference:
    """A paper mentioned in Slack, before anything has been looked up."""

    doi: str | None = None
    arxiv_id: str | None = None
    url: str | None = None
    source_ts: str = ""
    # Slack user ids @-mentioned in the same message. Someone naming a
    # colleague beside a link is asking them to read it, which is worth keeping
    # once the paper reaches the library.
    mentions: list[str] = field(default_factory=list)

    @property
    def identity(self) -> str:
        return self.doi or (f"arXiv:{self.arxiv_id}" if self.arxiv_id else self.url or "")


@dataclass
class Paper:
    """A resolved reference, ready to become a Zotero item."""

    title: str = ""
    authors: list[dict[str, str]] = field(default_factory=list)
    date: str = ""
    doi: str | None = None
    arxiv_id: str | None = None
    url: str = ""
    abstract: str = ""
    container: str = ""
    item_type: str = "journalArticle"

    def to_json(self) -> str:
        import json

        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, blob: str) -> Paper | None:
        import json

        try:
            return cls(**json.loads(blob))
        except Exception:
            return None

    def to_zotero(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "itemType": self.item_type,
            "title": self.title[:500],
            "creators": self.authors[:50],
            "abstractNote": self.abstract[:5000],
            "url": self.url,
            "date": self.date,
        }
        if self.doi:
            item["DOI"] = self.doi
        if self.item_type == "journalArticle":
            item["publicationTitle"] = self.container
        elif self.item_type == "preprint":
            item["repository"] = self.container or "arXiv"
            if self.arxiv_id:
                item["archiveID"] = f"arXiv:{self.arxiv_id}"
        return item


# ------------------------------------------------------------------ detection


def extract(text: str, source_ts: str = "") -> list[Reference]:
    """References in one message, de-duplicated, DOI preferred over URL.

    Every reference in a message inherits that message's @-mentions: someone
    posting three links and tagging a colleague means all three, not the last.
    """
    text = html.unescape(text or "")
    mentions = sorted(_MENTION_RE.findall(text))
    # Take the target of every Slack-formatted link, then drop the whole
    # construct so its display text cannot be re-matched as a second URL.
    linked = [normalise_url(u) for u in _SLACK_LINK_RE.findall(text)]
    text = _SLACK_LINK_RE.sub(" ", text) + " " + " ".join(linked)

    refs: list[Reference] = []
    seen: set[str] = set()

    def add(ref: Reference) -> None:
        if ref.identity and ref.identity.lower() not in seen:
            seen.add(ref.identity.lower())
            ref.mentions = list(mentions)
            refs.append(ref)

    for m in _ARXIV_RE.finditer(text):
        add(Reference(arxiv_id=m.group(1), url=f"https://arxiv.org/abs/{m.group(1)}",
                      source_ts=source_ts))
    for m in _ARXIV_BARE_RE.finditer(text):
        add(Reference(arxiv_id=m.group(1), url=f"https://arxiv.org/abs/{m.group(1)}",
                      source_ts=source_ts))
    for m in _DOI_RE.finditer(text):
        add(Reference(doi=clean_doi(m.group(1)), source_ts=source_ts))

    for url in _URL_RE.findall(text):
        url = normalise_url(url)
        doi = doi_from_url(url)
        if doi:
            add(Reference(doi=doi, url=url, source_ts=source_ts))
        elif "arxiv.org" not in url.lower():
            add(Reference(url=url, source_ts=source_ts))
    return refs


def doi_from_url(url: str) -> str | None:
    """A DOI recoverable from the URL alone, no network needed."""
    for pat in _DOI_FROM_URL:
        m = pat.search(url)
        if not m:
            continue
        got = m.group(1)
        # nature.com/articles/<slug> is not itself a DOI but maps to one.
        if not got.startswith("10."):
            return f"10.1038/{got}"
        return clean_doi(got)
    return None


# ----------------------------------------------------------------- resolution


async def resolve(session: aiohttp.ClientSession, ref: Reference) -> Paper | None:
    if ref.arxiv_id:
        return await _from_arxiv(session, ref.arxiv_id)
    if ref.doi:
        return await _from_crossref(session, ref.doi)
    return None


async def _from_crossref(session: aiohttp.ClientSession, doi: str) -> Paper | None:
    try:
        async with session.get(f"{CROSSREF}/{doi}", timeout=aiohttp.ClientTimeout(30)) as r:
            if r.status != 200:
                logger.info("Crossref has no record for %s (%s)", doi, r.status)
                return None
            msg = (await r.json())["message"]
    except Exception:
        logger.warning("Crossref lookup failed for %s", doi, exc_info=False)
        return None

    authors = [
        {"creatorType": "author",
         "firstName": (a.get("given") or "")[:80],
         "lastName": (a.get("family") or a.get("name") or "")[:80]}
        for a in msg.get("author", [])
        if a.get("family") or a.get("name")
    ]
    parts = (msg.get("issued", {}).get("date-parts") or [[]])[0]
    return Paper(
        title=strip_markup(" ".join(msg.get("title") or ["(untitled)"])),
        authors=authors,
        date="-".join(str(p) for p in parts) if parts else "",
        doi=doi,
        url=msg.get("URL", f"https://doi.org/{doi}"),
        abstract=strip_markup(msg.get("abstract") or ""),
        container=" ".join(msg.get("container-title") or []),
        item_type="journalArticle",
    )


async def _from_arxiv(session: aiohttp.ClientSession, arxiv_id: str) -> Paper | None:
    try:
        async with session.get(
            ARXIV_API, params={"id_list": arxiv_id, "max_results": "1"},
            timeout=aiohttp.ClientTimeout(30),
        ) as r:
            xml = await r.text()
    except Exception:
        logger.warning("arXiv lookup failed for %s", arxiv_id, exc_info=False)
        return None

    # The Atom feed opens with its own <title> echoing the query. Scope every
    # lookup to the <entry> block or every paper is titled "arXiv Query: ...".
    entry_m = re.search(r"<entry>(.*?)</entry>", xml, re.DOTALL)
    if not entry_m:
        return None
    entry = entry_m.group(1)

    def tag(name: str) -> str:
        m = re.search(rf"<{name}>(.*?)</{name}>", entry, re.DOTALL)
        return strip_markup(m.group(1)) if m else ""

    title = tag("title")
    if not title:
        return None
    authors = [
        {"creatorType": "author", "firstName": " ".join(n.split()[:-1]),
         "lastName": n.split()[-1]}
        for n in re.findall(r"<author>\s*<name>(.*?)</name>", entry, re.DOTALL)
        if n.split()
    ]
    doi_m = re.search(r"<arxiv:doi[^>]*>(.*?)</arxiv:doi>", entry, re.DOTALL)
    return Paper(
        title=title,
        authors=authors,
        date=tag("published")[:10],
        arxiv_id=arxiv_id,
        doi=doi_m.group(1).strip() if doi_m else None,
        url=f"https://arxiv.org/abs/{arxiv_id}",
        abstract=tag("summary"),
        container="arXiv",
        item_type="preprint",
    )


# ------------------------------------------------------------- open-access PDF


async def find_pdf(
    session: aiohttp.ClientSession, paper: Paper, email: str
) -> tuple[bytes, str] | None:
    """A legally-fetchable PDF for this paper, or None.

    arXiv is served directly. For anything else, Unpaywall is asked whether a
    legal open-access copy exists; a paywalled article simply returns nothing
    and is left for a human.
    """
    url = None
    if paper.arxiv_id:
        url = f"https://arxiv.org/pdf/{paper.arxiv_id}"
    elif paper.doi:
        url = await _unpaywall_pdf(session, paper.doi, email)
    if not url:
        return None

    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=60, sock_read=20),
            allow_redirects=True,
        ) as r:
            if r.status != 200:
                return None
            ctype = (r.headers.get("Content-Type") or "").lower()
            body = await r.read()
    except Exception:
        logger.info("PDF fetch failed for %s", paper.identity_hint(), exc_info=False)
        return None

    # Publishers commonly answer a PDF request with an HTML interstitial.
    if "pdf" not in ctype and not body.startswith(b"%PDF"):
        return None
    name = (paper.arxiv_id or (paper.doi or "paper").replace("/", "_")) + ".pdf"
    return body, name


async def _unpaywall_pdf(
    session: aiohttp.ClientSession, doi: str, email: str
) -> str | None:
    try:
        async with session.get(
            f"{UNPAYWALL}/{doi}", params={"email": email},
            timeout=aiohttp.ClientTimeout(30),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
    except Exception:
        return None
    loc = data.get("best_oa_location") or {}
    return loc.get("url_for_pdf") or None


def _identity_hint(self: Paper) -> str:
    return self.doi or self.arxiv_id or self.url or self.title[:40]


Paper.identity_hint = _identity_hint  # type: ignore[attr-defined]
