"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date

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
    from slackqa.answerer import CredentialsError
    from slackqa.app import build

    settings = get_settings()
    if not settings.channels:
        print("No channels configured. Set CHANNELS=C0123ABC,C0456DEF in .env")
        return 1
    try:
        bot = await build(settings)
    except CredentialsError as e:
        # A dead key is a config problem, not a crash: say so in one line
        # rather than burying it in a traceback.
        print(f"\nStartup aborted: {e}\n")
        return 2
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


async def _ask(channel_id: str, question: str) -> int:
    """Answer one question from the terminal — no Slack round trip."""
    from slackqa.app import build

    settings = get_settings()
    bot = await build(settings)
    assert bot.answerer is not None
    answer = await bot.answerer.answer(channel_id, question)
    print(answer.text)
    print(f"\n[{'refused' if answer.refused else 'answered'} | "
          f"{len(answer.chunk_ids)} chunks | {answer.searches} search(es)]")
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

    ask = sub.add_parser("ask", help="ask one question from the terminal")
    ask.add_argument("channel")
    ask.add_argument("question", nargs="+")

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
    if command == "run":
        code = asyncio.run(_run())
    elif command == "sync":
        code = asyncio.run(_sync())
    elif command == "stats":
        code = asyncio.run(_stats())
    elif command == "ask":
        code = asyncio.run(_ask(args.channel, " ".join(args.question)))
    elif command == "glossary":
        if not getattr(args, "action", None):
            print("Usage: slackqa glossary {list,html,update,endorse,add}")
            code = 1
        else:
            code = asyncio.run(_glossary(args.action, args))
    else:
        parser.print_help()
        code = 1

    sys.exit(code)


if __name__ == "__main__":
    main()
