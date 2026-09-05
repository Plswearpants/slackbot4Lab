"""Scan a channel for shared papers and file them into its Zotero collection.

One collection per channel, created on first use and named after the channel.
Every reference is recorded locally whatever its outcome, which gives two things
a shared library cannot: a re-scan skips what it already handled, and the
papers nobody could fetch are a list you can work through rather than a gap
you have to notice.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from slackqa.ingest import fetch_history
from slackqa.literature import Paper, Reference, extract, find_pdf, resolve
from slackqa.zotero import NEEDS_PDF_TAG, Attachment, Zotero, reader_tag

logger = logging.getLogger(__name__)

# arXiv asks for roughly one request every three seconds and throttles clients
# that ignore it — silently, by answering with no entry, which is
# indistinguishable from the paper not existing.
ARXIV_PAUSE_SECONDS = 3.0
CROSSREF_PAUSE_SECONDS = 0.2


@dataclass
class Outcome:
    identity: str
    status: str  # added | needs-pdf | unresolved | skipped
    title: str = ""
    detail: str = ""

    @property
    def emoji(self) -> str:
        return {"added": "OK  ", "needs-pdf": "LINK", "unresolved": "??  ",
                "skipped": "--  "}.get(self.status, "    ")


async def scan_channel(
    store,
    slack_client,
    zot: Zotero,
    channel_id: str,
    channel_name: str,
    *,
    unpaywall_email: str,
    limit: int = 10,
    dry_run: bool = False,
    oldest_first: bool = False,
    names=None,
    fetch_pdfs: bool = True,
) -> list[Outcome]:
    """File up to ``limit`` newly-filed papers from this channel.

    Reads the whole channel: conversations.history returns one page, so an
    unpaginated fetch quietly scans only the most recent slice — which left
    four hundred already-resolved papers unfiled and looked like the run
    finishing early.
    """
    messages = await fetch_history(slack_client, channel_id)

    refs: list[Reference] = []
    seen: set[str] = set()
    # Newest first by default: a capped run then files what the group is
    # reading now. Already-handled references are skipped on every run, so
    # either order still advances through the backlog across repeated scans.
    for m in sorted(
        messages, key=lambda m: float(m.get("ts", 0)), reverse=not oldest_first
    ):
        for ref in extract(m.get("text") or "", source_ts=str(m.get("ts", ""))):
            if ref.identity and ref.identity.lower() not in seen:
                seen.add(ref.identity.lower())
                refs.append(ref)

    logger.info("channel=%s: %d distinct references found", channel_name, len(refs))

    # Resolve every mentioned id once. A paper pointed at three people should
    # not cost three lookups, and the same colleague recurs across papers.
    mentioned = {uid for r in refs for uid in r.mentions}
    display = await names.resolve(mentioned) if (names and mentioned) else {}

    def reader_tags(ref: Reference) -> list[str]:
        return [reader_tag(display.get(uid, uid)) for uid in ref.mentions]

    stored = await store.stored_metadata(channel_id)
    if stored:
        logger.info("Reusing %d already-resolved records", len(stored))

    collection = None if dry_run else await zot.get_or_create_collection(channel_name)
    existing_doi: dict[str, str] = {}
    existing_title: dict[str, str] = {}
    if not dry_run:
        existing_doi, existing_title = await zot.index_existing()
        logger.info("Library already holds %d items", len(existing_doi))
    outcomes: list[Outcome] = []
    added = 0

    async with aiohttp.ClientSession(
        headers={"User-Agent": f"slackqa (mailto:{unpaywall_email})"},
        timeout=aiohttp.ClientTimeout(total=120, connect=15, sock_read=45),
    ) as session:
        for ref in refs:
            if added >= limit:
                break
            logger.info("[%d/%d] %s", added + 1, limit, ref.identity[:70])
            if await store.filed_reference(ref.identity):
                # Already filed, but someone may have pointed it at a colleague
                # since — or on a message this scan reached first. Tag the
                # existing item rather than skipping silently.
                if not dry_run and ref.mentions:
                    existing_key = await store.zotero_key_for(ref.identity)
                    if existing_key:
                        for tag in reader_tags(ref):
                            await zot.add_tag(existing_key, tag)
                continue

            blob = stored.get(ref.identity)
            paper = Paper.from_json(blob) if blob else None
            if paper is None:
                paper = await resolve(session, ref)
                await asyncio.sleep(
                    ARXIV_PAUSE_SECONDS if ref.arxiv_id else CROSSREF_PAUSE_SECONDS
                )
            if paper is None:
                # A bare link to something that is not a paper, or a DOI no
                # registry knows. Recorded so it is not retried every scan;
                # `lit resolve --retry-failed` clears these deliberately.
                outcomes.append(Outcome(ref.identity, "unresolved",
                                        detail="no metadata from Crossref or arXiv"))
                if not dry_run:
                    await store.record_reference(
                        ref.identity, channel_id, "unresolved",
                        detail="no metadata", source_ts=ref.source_ts,
                    )
                added += 1
                continue

            if dry_run:
                readers = reader_tags(ref)
                outcomes.append(Outcome(
                    ref.identity, "added", title=paper.title,
                    detail="(dry run)" + (f" · {', '.join(readers)}" if readers else ""),
                ))
                added += 1
                continue

            existing = (
                existing_doi.get((paper.doi or "").lower())
                or existing_title.get(" ".join(paper.title.lower().split()))
            )
            if existing:
                await store.record_reference(
                    ref.identity, channel_id, "added", title=paper.title,
                    zotero_key=existing, detail="already in the group library",
                    source_ts=ref.source_ts,
                )
                outcomes.append(Outcome(ref.identity, "skipped", title=paper.title,
                                        detail="already in library"))
                continue

            item = paper.to_zotero()
            tags = list(item.get("tags") or [])
            tags += [{"tag": t} for t in reader_tags(ref)]
            item["tags"] = tags
            item_key = await zot.create_item(item, collection)
            if paper.doi:
                existing_doi[paper.doi.lower()] = item_key
            existing_title[" ".join(paper.title.lower().split())] = item_key
            # Recorded before the PDF work, which is the slow, failure-prone
            # part: an interruption then leaves a true record of what exists in
            # Zotero rather than losing the item entirely.
            await store.record_reference(
                ref.identity, channel_id, "needs-pdf", title=paper.title,
                abstract=paper.abstract, zotero_key=item_key,
                detail="item created, PDF pending", source_ts=ref.source_ts,
            )

            found = (
                await find_pdf(session, paper, unpaywall_email)
                if fetch_pdfs
                else None
            )
            if found:
                content, filename = found
                await zot.upload_pdf(item_key, Attachment(filename, content))
                await store.record_reference(
                    ref.identity, channel_id, "added", title=paper.title,
                    abstract=paper.abstract, zotero_key=item_key, has_pdf=True,
                    source_ts=ref.source_ts,
                )
                readers = reader_tags(ref)
                outcomes.append(Outcome(
                    ref.identity, "added", title=paper.title,
                    detail=f"PDF {len(content) // 1024} KB"
                    + (f" · {', '.join(readers)}" if readers else ""),
                ))
            else:
                # No legal open-access copy. Leave a link and flag it for a
                # human with the Zotero Connector, which runs in their own
                # authenticated browser.
                if paper.url:
                    await zot.add_link_attachment(item_key, paper.url, "Publisher page")
                await zot.add_tag(item_key, NEEDS_PDF_TAG)
                await store.record_reference(
                    ref.identity, channel_id, "needs-pdf", title=paper.title,
                    abstract=paper.abstract, zotero_key=item_key,
                    detail="PDFs skipped" if not fetch_pdfs else "no open-access PDF",
                    source_ts=ref.source_ts,
                )
                readers = reader_tags(ref)
                outcomes.append(Outcome(
                    ref.identity, "needs-pdf", title=paper.title,
                    detail="metadata + link only"
                    + (f" · {', '.join(readers)}" if readers else ""),
                ))
            added += 1

    return outcomes


async def resolve_metadata(
    store,
    slack_client,
    channel_id: str,
    *,
    unpaywall_email: str,
    limit: int | None = None,
) -> tuple[int, int]:
    """Resolve titles and abstracts for every paper linked in a channel.

    Metadata only: no Zotero writes, no PDF downloads, no credentials. The
    point is search, not filing — a channel of bare links indexes as a wall of
    URLs, and this is what puts the papers' own words where retrieval can see
    them. Returns (resolved, failed).
    """
    messages = await fetch_history(slack_client, channel_id)
    refs: list[Reference] = []
    seen: set[str] = set()
    for m in messages:
        for ref in extract(m.get("text") or "", source_ts=str(m.get("ts", ""))):
            if ref.identity and ref.identity.lower() not in seen:
                seen.add(ref.identity.lower())
                refs.append(ref)

    known = await store.resolved_papers(channel_id)
    todo = [r for r in refs if r.identity not in known and (r.doi or r.arxiv_id)]
    if limit:
        todo = todo[:limit]
    logger.info(
        "%s: %d references, %d already known, resolving %d",
        channel_id, len(refs), len(known), len(todo),
    )

    resolved = failed = 0
    async with aiohttp.ClientSession(
        headers={"User-Agent": f"slackqa (mailto:{unpaywall_email})"},
        timeout=aiohttp.ClientTimeout(total=45, connect=10, sock_read=20),
    ) as session:
        for i, ref in enumerate(todo, start=1):
            paper = await resolve(session, ref)
            if paper and paper.title:
                await store.record_reference(
                    ref.identity, channel_id, "indexed", title=paper.title,
                    abstract=paper.abstract, metadata=paper.to_json(),
                    source_ts=ref.source_ts,
                )
                resolved += 1
            else:
                failed += 1
            await asyncio.sleep(
                ARXIV_PAUSE_SECONDS if ref.arxiv_id else CROSSREF_PAUSE_SECONDS
            )
            if i % 25 == 0:
                logger.info("  %d/%d (%d resolved)", i, len(todo), resolved)
    return resolved, failed


def format_report(outcomes: list[Outcome], collection: str) -> str:
    if not outcomes:
        return "No new papers found."
    lines = [f"Collection: {collection}", ""]
    for o in outcomes:
        lines.append(f"{o.emoji} {o.title[:62] or o.identity[:62]}")
        lines.append(f"     {o.identity[:70]}  {o.detail}")
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    lines.append("")
    lines.append("  ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if counts.get("needs-pdf"):
        lines.append("")
        lines.append("Papers tagged 'needs-pdf' have metadata and a link but no file.")
        lines.append("Open them with the Zotero Connector: slackqa lit pending")
    return "\n".join(lines)
