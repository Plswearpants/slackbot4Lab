"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date

from slackqa.answerer import CredentialsError
from slackqa.config import get_settings


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)


async def _run() -> int:
    from slackqa.app import build

    settings = get_settings()
    if not settings.channels:
        print("No channels configured. Set CHANNELS=C0123ABC,C0456DEF in .env")
        return 1
    bot = await build(settings)
    await bot.sync_all()
    await bot.start()
    return 0


async def _sync() -> int:
    from slackqa.app import build

    settings = get_settings()
    bot = await build(settings)
    await bot.sync_all()
    await bot.store.close()
    return 0


async def _ask(channel_id: str, question: str, deep: bool = False) -> int:
    """Answer one question from the terminal — no Slack round trip."""
    from slackqa.app import build

    settings = get_settings()
    bot = await build(settings)
    assert bot.answerer is not None
    answer = await bot.answerer.answer(channel_id, question, deep=deep)
    print(answer.text)
    print(f"\n[{'refused' if answer.refused else 'answered'} | "
          f"{len(answer.chunk_ids)} chunks | {answer.searches} search(es)"
          f"{' | deep' if answer.deep else ''}]")
    await bot.store.close()
    return 0


async def _glossary(action: str, args) -> int:
    from slackqa.glossary import Entry, Glossary, render_html

    settings = get_settings()
    g = Glossary.load(settings.glossary_path)

    if action == "list":
        if not g.entries:
            print(f"No terms yet. Run 'slackqa glossary update' or edit {settings.glossary_path}")
            return 0
        for e in sorted(g.entries, key=lambda x: x.term.lower()):
            mark = "OK " if e.endorsed else "?? "
            alias = f"  (aka {', '.join(e.aliases)})" if e.aliases else ""
            scope = f"  [{', '.join(e.channels)}]" if e.channels else "  [all channels]"
            print(f"{mark}{e.term}{alias}{scope}\n     {e.definition[:90]}")
        n_ok = sum(1 for e in g.entries if e.endorsed)
        print(f"\n{len(g.entries)} terms · {n_ok} endorsed · {len(g.entries) - n_ok} unendorsed")
        return 0

    if action == "html":
        from slackqa.app import build

        bot = await build(settings)
        await bot.write_glossary_html()
        await bot.store.close()
        print(f"Wrote {settings.glossary_html_path}")
        return 0

    if action == "endorse":
        if not g.endorse(args.term, args.by):
            print(f"No such term: {args.term}")
            return 1
        g.save()
        path = settings.glossary_html_path
        if path.exists():
            path.write_text(render_html(g.entries))
        print(f"Endorsed '{args.term}' as {args.by}")
        return 0

    if action == "add":
        definition = " ".join(args.definition)
        if not g.add(Entry(term=args.term, definition=definition,
                           channels=[args.channel] if args.channel else [],
                           endorsed_by=f"{args.by} ({date.today().isoformat()})")):
            print(f"'{args.term}' already exists (or matches an alias)")
            return 1
        g.save()
        print(f"Added '{args.term}'")
        return 0

    if action == "update":
        from slackqa.app import build

        bot = await build(settings)
        added = await bot.run_mining()
        print(f"Added {len(added)} term(s): {', '.join(added) if added else '(none)'}")
        print(f"Review at {settings.glossary_html_path}")
        await bot.store.close()
        return 0

    return 1


async def _lit(action: str, args) -> int:
    from slackqa.store import Store

    settings = get_settings()
    store = await Store.open(settings.db_path)

    if action == "pending":
        rows = await store.literature_by_status("needs-pdf")
        counts = await store.literature_counts()
        if not rows:
            print("Nothing waiting for a PDF.")
        for r in rows:
            print(f"  {r['title'][:66]}")
            print(f"     {r['identity']}")
        print()
        print("  ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "(nothing yet)")
        if rows:
            print("\nOpen these in a browser and save with the Zotero Connector —")
            print("it uses your own session, so it reaches what a server should not.")
        await store.close()
        return 0

    if action == "scan":
        from slack_sdk.web.async_client import AsyncWebClient

        from slackqa.librarian import format_report, scan_channel
        from slackqa.zotero import Zotero

        if not settings.literature_channels:
            print("No LITERATURE_CHANNELS configured.")
            await store.close()
            return 1
        if not (settings.zotero_api_key and settings.zotero_group_id):
            print("Set ZOTERO_API_KEY and ZOTERO_GROUP_ID in .env")
            await store.close()
            return 1

        dry = args.dry_run
        if not dry and not settings.zotero_write_enabled:
            print("Writing is off. Set ZOTERO_WRITE_ENABLED=true in .env, or")
            print("re-run with --dry-run to see what would be filed.")
            await store.close()
            return 1

        slack = AsyncWebClient(token=settings.slack_bot_token)
        zot = Zotero(settings.zotero_api_key, settings.zotero_group_id)
        limit = args.limit or settings.literature_max_items

        for channel_id in settings.literature_channels:
            info = (await slack.conversations_info(channel=channel_id))["channel"]
            name = info.get("name") or channel_id
            outcomes = await scan_channel(
                store, slack, zot, channel_id, name,
                unpaywall_email=settings.unpaywall_email,
                limit=limit, dry_run=dry, oldest_first=args.oldest_first,
            )
            print(format_report(outcomes, name + (" (dry run)" if dry else "")))
        await store.close()
        return 0

    await store.close()
    return 1


async def _eval() -> int:
    """Measure retrieval recall. No API key needed: embeddings are local."""
    from slackqa.embeddings import FastEmbedEmbedder
    from slackqa.evals import format_report, load_cases, run
    from slackqa.glossary import Glossary
    from slackqa.retrieval import Retriever
    from slackqa.store import Store

    settings = get_settings()
    cases = load_cases(settings.evals_path)
    if not cases:
        print(f"No eval cases at {settings.evals_path}")
        return 1

    store = await Store.open(settings.db_path)
    retriever = Retriever(
        store,
        FastEmbedEmbedder(settings.embed_model),
        candidates=settings.candidates_per_retriever,
        rrf_k=settings.rrf_k,
        min_cosine=settings.relevance_threshold,
    )
    glossary = Glossary.load(settings.glossary_path) if settings.glossary_enabled else None
    results = await run(retriever, cases, settings.top_k, glossary)
    await store.close()

    print(format_report(results, settings.top_k))
    return 0 if all(r.passed for r in results) else 1


async def _status() -> int:
    """Print the three dashboard indicators without a browser."""
    import aiohttp

    settings = get_settings()
    url = f"http://{settings.dashboard_host}:{settings.dashboard_port}/health"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json()
    except Exception:
        print(f"Listener        UNKNOWN  no response from {url}")
        print("                         Either no listener is running, or one is")
        print("                         running without its dashboard because the")
        print("                         port was already taken at startup.")
        print("Index           unknown  (cannot reach the listener)")
        print("API key         unknown  (cannot reach the listener)")
        return 2

    mark = lambda ok: "OK  " if ok else "DOWN"  # noqa: E731
    lis, idx, key = d["listener"], d["index"], d["api_key"]
    print(f"Listener        {mark(lis['ok'])}     {lis['state']}, "
          f"up {lis['uptime']}, last event {lis['last_event']}")
    print(f"Index           {mark(idx['ok'])}     newest message {idx['newest_ago']}, "
          f"last sync {idx['last_sync']}")
    for c in idx["channels"]:
        print(f"                         #{c['channel']}: {c['messages']} messages, "
              f"{c['chunks']} chunks, newest {c['newest_ago']}")
    print(f"API key         {mark(key['ok'])}     {key['model']}: {key['detail']} "
          f"(checked {key['checked']})")
    if key.get("stale"):
        print("                         A different key is in .env — restart to use it.")
    return 0 if (lis["ok"] and idx["ok"] and key["ok"]) else 1


async def _stats() -> int:
    from slackqa.store import Store

    settings = get_settings()
    store = await Store.open(settings.db_path)
    for channel_id in settings.channels:
        msgs = await store.messages_in_range(channel_id, 0, float("inf"))
        ids, _ = await store.embeddings_for_channel(channel_id)
        users = await store.distinct_users(channel_id)
        done = await store.is_backfilled(channel_id)
        print(
            f"{channel_id}: {len(msgs):>6} messages  {len(ids):>5} chunks  "
            f"{len(users):>4} people  backfilled={done}"
        )
    await store.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="slackqa", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="sync, then listen for questions (default)")
    sub.add_parser("sync", help="backfill / catch up / reconcile, then exit")
    sub.add_parser("stats", help="show what is indexed")
    sub.add_parser("status", help="listener / index / API key indicators")
    sub.add_parser("eval", help="retrieval recall against evals/retrieval.yaml")

    lit = sub.add_parser("lit", help="file shared papers into Zotero")
    lsub = lit.add_subparsers(dest="action")
    ls = lsub.add_parser("scan", help="scan whitelisted channels for papers")
    ls.add_argument("--limit", type=int, help="max new papers this run")
    ls.add_argument("--dry-run", action="store_true", help="resolve but write nothing")
    ls.add_argument("--oldest-first", action="store_true",
                    help="work forward from the oldest papers instead of the newest")
    lsub.add_parser("pending", help="papers needing a human to fetch the PDF")

    ask = sub.add_parser("ask", help="ask one question from the terminal")
    ask.add_argument("channel")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--deep", action="store_true",
                     help="expand the query with an LLM before searching")

    gl = sub.add_parser("glossary", help="shared vocabulary the bot consults")
    gsub = gl.add_subparsers(dest="action")
    gsub.add_parser("list", help="show all terms")
    gsub.add_parser("html", help="regenerate the HTML view")
    gsub.add_parser("update", help="mine the index for undefined jargon")
    ge = gsub.add_parser("endorse", help="mark a definition as human-approved")
    ge.add_argument("term")
    ge.add_argument("--by", required=True, help="who is endorsing it")
    ga = gsub.add_parser("add", help="add a term by hand")
    ga.add_argument("term")
    ga.add_argument("definition", nargs="+")
    ga.add_argument("--by", required=True, help="who is defining it")
    ga.add_argument("--channel", help="scope to one channel (default: all channels)")

    args = parser.parse_args()
    _setup_logging(args.verbose)

    command = args.command or "run"
    try:
        code = _dispatch(command, args, parser)
    except CredentialsError as e:
        # A dead key is a config problem, not a crash. Every command builds the
        # app and so can hit this; handling it once here means none of them can
        # dump a traceback at the user.
        print(f"\n{e}\n")
        code = 2
    sys.exit(code)


def _dispatch(command: str, args, parser) -> int:
    if command == "run":
        code = asyncio.run(_run())
    elif command == "sync":
        code = asyncio.run(_sync())
    elif command == "stats":
        code = asyncio.run(_stats())
    elif command == "status":
        code = asyncio.run(_status())
    elif command == "eval":
        code = asyncio.run(_eval())
    elif command == "lit":
        if not getattr(args, "action", None):
            print("Usage: slackqa lit {scan,pending}")
            code = 1
        else:
            code = asyncio.run(_lit(args.action, args))
    elif command == "ask":
        code = asyncio.run(_ask(args.channel, " ".join(args.question), args.deep))
    elif command == "glossary":
        if not getattr(args, "action", None):
            print("Usage: slackqa glossary {list,html,update,endorse,add}")
            code = 1
        else:
            code = asyncio.run(_glossary(args.action, args))
    else:
        parser.print_help()
        code = 1
    return code


if __name__ == "__main__":
    main()
