"""Scan a channel for shared papers and file them into its Zotero collection.

One collection per channel, created on first use and named after the channel.
Every reference is recorded locally whatever its outcome, which gives two things
a shared library cannot: a re-scan skips what it already handled, and the
papers nobody could fetch are a list you can work through rather than a gap
you have to notice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

from slackqa.literature import Reference, extract, find_pdf, resolve
from slackqa.zotero import NEEDS_PDF_TAG, Attachment, Zotero

logger = logging.getLogger(__name__)


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
    history_limit: int = 400,
    dry_run: bool = False,
    oldest_first: bool = False,
) -> list[Outcome]:
    """File up to ``limit`` newly-seen papers from this channel."""
    resp = await slack_client.conversations_history(
        channel=channel_id, limit=min(history_limit, 1000)
    )
    messages = resp.get("messages") or []

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

    collection = None if dry_run else await zot.get_or_create_collection(channel_name)
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
            if await store.seen_reference(ref.identity):
                continue

            paper = await resolve(session, ref)
            if paper is None:
                # A bare link to something that is not a paper, or a DOI no
                # registry knows. Recorded so it is not retried every scan.
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
                outcomes.append(Outcome(ref.identity, "added", title=paper.title,
                                        detail="(dry run)"))
                added += 1
                continue

            existing = (
                await zot.find_by_doi(paper.doi) if paper.doi else None
            ) or await zot.find_by_title(paper.title)
            if existing:
                await store.record_reference(
                    ref.identity, channel_id, "added", title=paper.title,
                    zotero_key=existing, detail="already in the group library",
                    source_ts=ref.source_ts,
                )
                outcomes.append(Outcome(ref.identity, "skipped", title=paper.title,
                                        detail="already in library"))
                continue

            item_key = await zot.create_item(paper.to_zotero(), collection)
            # Recorded before the PDF work, which is the slow, failure-prone
            # part: an interruption then leaves a true record of what exists in
            # Zotero rather than losing the item entirely.
            await store.record_reference(
                ref.identity, channel_id, "needs-pdf", title=paper.title,
                zotero_key=item_key, detail="item created, PDF pending",
                source_ts=ref.source_ts,
            )

            found = await find_pdf(session, paper, unpaywall_email)
            if found:
                content, filename = found
                await zot.upload_pdf(item_key, Attachment(filename, content))
                await store.record_reference(
                    ref.identity, channel_id, "added", title=paper.title,
                    zotero_key=item_key, has_pdf=True, source_ts=ref.source_ts,
                )
                outcomes.append(Outcome(ref.identity, "added", title=paper.title,
                                        detail=f"PDF {len(content) // 1024} KB"))
            else:
                # No legal open-access copy. Leave a link and flag it for a
                # human with the Zotero Connector, which runs in their own
                # authenticated browser.
                if paper.url:
                    await zot.add_link_attachment(item_key, paper.url, "Publisher page")
                await zot.add_tag(item_key, NEEDS_PDF_TAG)
                await store.record_reference(
                    ref.identity, channel_id, "needs-pdf", title=paper.title,
                    zotero_key=item_key, detail="no open-access PDF",
                    source_ts=ref.source_ts,
                )
                outcomes.append(Outcome(ref.identity, "needs-pdf", title=paper.title,
                                        detail="metadata + link only"))
            added += 1

    return outcomes


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
