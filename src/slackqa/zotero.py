"""Zotero Web API v3 client, scoped to one group library.

Writes land in a library the whole lab shares, so every item this creates is
tagged (see ``BOT_TAG``) — a bad run is then one saved search away from being
found and undone, rather than something a person has to spot by eye among their
own references.

File upload is the one genuinely awkward corner of the API: registering an
attachment, asking for upload authorisation, PUTting the bytes to storage, then
confirming. It is worth doing rather than only linking, because arXiv alone
accounts for most of what this lab shares and those PDFs are freely fetchable.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

API = "https://api.zotero.org"

# Without an explicit timeout aiohttp allows five minutes per request, so one
# stalled call silently costs more than the whole rest of a scan.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45)
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=180)

# Every item the bot creates carries this, so its work is reversible: search the
# group for the tag to review or delete everything it added.
BOT_TAG = "added-by:LAIRbot"

# Marks an item whose PDF is behind a paywall we will not automate around.
NEEDS_PDF_TAG = "needs-pdf"


class ZoteroError(RuntimeError):
    pass


@dataclass
class Attachment:
    filename: str
    content: bytes
    content_type: str = "application/pdf"

    @property
    def md5(self) -> str:
        return hashlib.md5(self.content).hexdigest()

    @property
    def mtime(self) -> int:
        """Modification time in milliseconds, which Zotero requires.

        Seconds, or a zero placeholder, are rejected with the unhelpful
        "File modification time not provided".
        """
        return int(time.time() * 1000)


class Zotero:
    def __init__(self, api_key: str, group_id: str | int) -> None:
        self._key = api_key
        self._group = str(group_id)
        self._base = f"{API}/groups/{self._group}"

    def _headers(self, **extra: str) -> dict[str, str]:
        h = {"Zotero-API-Version": "3", "Zotero-API-Key": self._key}
        h.update(extra)
        return h

    async def _request(
        self, session: aiohttp.ClientSession, method: str, path: str, **kw: Any
    ) -> Any:
        url = path if path.startswith("http") else f"{self._base}{path}"
        headers = {**self._headers(), **kw.pop("headers", {})}
        kw.setdefault("timeout", REQUEST_TIMEOUT)
        async with session.request(method, url, headers=headers, **kw) as r:
            body = await r.text()
            if r.status >= 400:
                raise ZoteroError(f"{method} {path} -> {r.status}: {body[:300]}")
            if not body:
                return None
            try:
                import json

                return json.loads(body)
            except ValueError:
                return body

    # ------------------------------------------------------------- read paths

    async def whoami(self) -> dict[str, Any]:
        async with aiohttp.ClientSession() as s:
            return await self._request(s, "GET", f"{API}/keys/{self._key}")

    async def collections(self) -> list[dict[str, Any]]:
        async with aiohttp.ClientSession() as s:
            return await self._request(s, "GET", "/collections?limit=100") or []

    async def get_or_create_collection(self, name: str) -> str:
        """Collection key for ``name``, creating it if absent.

        Matched case-insensitively so a collection someone made by hand as
        "CoolPapers" is reused rather than duplicated as "coolpapers".
        """
        for c in await self.collections():
            if c["data"]["name"].strip().lower() == name.strip().lower():
                return c["data"]["key"]

        async with aiohttp.ClientSession() as s:
            resp = await self._request(
                s, "POST", "/collections", json=[{"name": name, "parentCollection": False}]
            )
        created = (resp or {}).get("successful", {})
        if not created:
            raise ZoteroError(f"Could not create collection {name!r}: {resp}")
        key = next(iter(created.values()))["key"]
        logger.info("Created Zotero collection %r (%s)", name, key)
        return key

    async def find_by_doi(self, doi: str) -> str | None:
        """Existing item key for this DOI, so a re-scan does not duplicate."""
        async with aiohttp.ClientSession() as s:
            items = await self._request(
                s, "GET", "/items", params={"q": doi, "qmode": "everything", "limit": "25"}
            )
        for it in items or []:
            if (it.get("data", {}).get("DOI") or "").lower() == doi.lower():
                return it["data"]["key"]
        return None

    async def find_by_title(self, title: str) -> str | None:
        if not title:
            return None
        async with aiohttp.ClientSession() as s:
            items = await self._request(
                s, "GET", "/items", params={"q": title[:120], "limit": "25"}
            )
        want = " ".join(title.lower().split())
        for it in items or []:
            got = " ".join((it.get("data", {}).get("title") or "").lower().split())
            if got and got == want:
                return it["data"]["key"]
        return None

    # ------------------------------------------------------------ write paths

    async def create_item(self, item: dict[str, Any], collection_key: str) -> str:
        payload = dict(item)
        payload.setdefault("collections", [])
        if collection_key not in payload["collections"]:
            payload["collections"] = [*payload["collections"], collection_key]
        tags = list(payload.get("tags") or [])
        if not any(t.get("tag") == BOT_TAG for t in tags):
            tags.append({"tag": BOT_TAG})
        payload["tags"] = tags

        async with aiohttp.ClientSession() as s:
            resp = await self._request(s, "POST", "/items", json=[payload])
        ok = (resp or {}).get("successful", {})
        if not ok:
            raise ZoteroError(f"Item not created: {(resp or {}).get('failed')}")
        return next(iter(ok.values()))["key"]

    async def add_link_attachment(self, parent_key: str, url: str, title: str) -> str:
        """A URL-only attachment — the fallback when we cannot fetch the file."""
        att = {
            "itemType": "attachment",
            "linkMode": "linked_url",
            "parentItem": parent_key,
            "title": title[:200] or "Full text",
            "url": url,
            "tags": [{"tag": BOT_TAG}],
        }
        async with aiohttp.ClientSession() as s:
            resp = await self._request(s, "POST", "/items", json=[att])
        ok = (resp or {}).get("successful", {})
        if not ok:
            raise ZoteroError(f"Link attachment failed: {(resp or {}).get('failed')}")
        return next(iter(ok.values()))["key"]

    async def add_tag(self, item_key: str, tag: str) -> None:
        async with aiohttp.ClientSession() as s:
            item = await self._request(s, "GET", f"/items/{item_key}")
            data = item["data"]
            tags = list(data.get("tags") or [])
            if any(t.get("tag") == tag for t in tags):
                return
            tags.append({"tag": tag})
            await self._request(
                s,
                "PATCH",
                f"/items/{item_key}",
                json={"tags": tags},
                headers={"If-Unmodified-Since-Version": str(item["version"])},
            )

    async def upload_pdf(self, parent_key: str, att: Attachment) -> str | None:
        """Attach a real file. Returns the attachment key, or None if skipped.

        Four steps, as the API requires: create the attachment item, request
        upload authorisation, PUT the bytes to storage, then confirm. Zotero may
        answer the authorisation step with ``exists``, meaning an identical file
        is already stored — that is a success, not a failure.
        """
        item = {
            "itemType": "attachment",
            "linkMode": "imported_url",
            "parentItem": parent_key,
            "title": att.filename,
            "filename": att.filename,
            "contentType": att.content_type,
            "tags": [{"tag": BOT_TAG}],
        }
        async with aiohttp.ClientSession() as s:
            resp = await self._request(s, "POST", "/items", json=[item])
            ok = (resp or {}).get("successful", {})
            if not ok:
                raise ZoteroError(f"Attachment item failed: {(resp or {}).get('failed')}")
            key = next(iter(ok.values()))["key"]

            auth = await self._request(
                s,
                "POST",
                f"/items/{key}/file",
                data={
                    "md5": att.md5,
                    "filename": att.filename,
                    "filesize": str(len(att.content)),
                    "mtime": str(att.mtime),
                    "contentType": att.content_type,
                    "params": "1",
                },
                headers={
                    "If-None-Match": "*",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if isinstance(auth, dict) and auth.get("exists"):
                logger.info("Zotero already stores this file; reusing")
                return key

            form = aiohttp.FormData()
            for k, v in (auth.get("params") or {}).items():
                form.add_field(k, str(v))
            form.add_field("file", att.content, filename=att.filename,
                           content_type=att.content_type)
            async with s.post(auth["url"], data=form, timeout=UPLOAD_TIMEOUT) as up:
                if up.status not in (200, 201, 204):
                    raise ZoteroError(f"Storage upload failed: {up.status}")

            await self._request(
                s,
                "POST",
                f"/items/{key}/file",
                data={"upload": auth["uploadKey"]},
                headers={
                    "If-None-Match": "*",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        return key
