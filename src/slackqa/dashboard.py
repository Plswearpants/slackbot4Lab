"""A small status page served by the listener itself.

Three questions, which are the ones you actually ask when something looks wrong:
is the listener up, how fresh is the index, and is the model key still good.

Served from inside the listener process rather than as a separate command, so
there is nothing extra to remember to start. The page polls ``/health`` with
JavaScript instead of reloading, which matters for the first indicator: when the
listener dies under an already-open page, the fetch fails and the card turns red,
rather than the page silently going stale.

Bound to localhost by default. It exposes channel names, message counts and
glossary state — not secret, but not for the network either.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# The key probe costs an HTTP round trip, so a browser refreshing every few
# seconds must not turn into a request per refresh.
KEY_CHECK_TTL_SECONDS = 60.0


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m ago"
    return f"{int(seconds // 86400)}d {int((seconds % 86400) // 3600)}h ago"


def _duration(seconds: float) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _stamp(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


@dataclass
class Runtime:
    """Facts only the running process knows."""

    started_at: float = field(default_factory=time.time)
    last_event_at: float | None = None
    last_question_at: float | None = None
    last_sync_at: float | None = None
    last_mining_at: float | None = None

    def note_event(self) -> None:
        self.last_event_at = time.time()

    def note_question(self) -> None:
        self.last_question_at = time.time()


class StatusProbe:
    """Collects the three indicators. Owns no state beyond a key-check cache."""

    def __init__(self, bot) -> None:
        self._bot = bot
        self._key_ok: bool | None = None
        self._key_detail = "not checked yet"
        self._key_checked_at: float | None = None

    # ------------------------------------------------------------ indicators

    def listener(self) -> dict[str, Any]:
        bot = self._bot
        connected = False
        try:
            client = getattr(bot.handler, "client", None)
            connected = bool(client and client.is_connected())
        except Exception:  # pragma: no cover - defensive
            connected = False

        now = time.time()
        return {
            "ok": connected,
            # The process is answering this request, so it is alive by
            # definition; the real signal is whether the websocket is up.
            "state": "connected" if connected else "process alive, socket down",
            "uptime": _duration(now - bot.runtime.started_at),
            "started": _stamp(bot.runtime.started_at),
            "last_event": _ago(
                now - bot.runtime.last_event_at if bot.runtime.last_event_at else None
            ),
            "last_question": _ago(
                now - bot.runtime.last_question_at
                if bot.runtime.last_question_at
                else None
            ),
            "channels": len(bot.settings.channels),
        }

    async def index(self) -> dict[str, Any]:
        store = self._bot.store
        names = await self._bot.channel_names()
        now = time.time()
        rows = []
        newest_overall: float | None = None

        for ch in self._bot.settings.channels:
            msgs = await store.messages_in_range(ch, 0, float("inf"))
            ids, _ = await store.embeddings_for_channel(ch)
            newest = max((m.ts_num for m in msgs), default=None)
            if newest and (newest_overall is None or newest > newest_overall):
                newest_overall = newest
            rows.append(
                {
                    "channel": names.get(ch, ch),
                    "id": ch,
                    "messages": len(msgs),
                    "chunks": len(ids),
                    "newest": _stamp(newest),
                    "newest_ago": _ago(now - newest if newest else None),
                    "backfilled": await store.is_backfilled(ch),
                }
            )

        return {
            # "Fresh" is deliberately generous: a quiet channel is not a broken
            # one, so this reports recency rather than judging it.
            "ok": newest_overall is not None,
            "newest": _stamp(newest_overall),
            "newest_ago": _ago(now - newest_overall if newest_overall else None),
            "last_sync": _ago(
                now - self._bot.runtime.last_sync_at
                if self._bot.runtime.last_sync_at
                else None
            ),
            "channels": rows,
            "glossary_terms": len(self._bot.glossary.entries),
            "glossary_endorsed": sum(
                1 for e in self._bot.glossary.entries if e.endorsed
            ),
        }

    async def api_key(self, force: bool = False) -> dict[str, Any]:
        now = time.time()
        fresh = (
            self._key_checked_at is not None
            and now - self._key_checked_at < KEY_CHECK_TTL_SECONDS
        )
        if not fresh or force:
            completer = self._bot.completer
            if completer is None:
                self._key_ok, self._key_detail = False, "no completer configured"
            else:
                try:
                    await completer.check_credentials()
                    self._key_ok, self._key_detail = True, "accepted"
                except Exception as e:
                    self._key_ok = False
                    self._key_detail = str(e).split("\n")[0][:160]
            self._key_checked_at = now

        return {
            "ok": bool(self._key_ok),
            "detail": self._key_detail,
            "model": self._bot.settings.model,
            "stale": self._key_is_stale(),
            "checked": _ago(
                now - self._key_checked_at if self._key_checked_at else None
            ),
        }

    def _key_is_stale(self) -> bool:
        """True when .env holds a different key than this process is using.

        Settings are read once at startup, so replacing a dead key in .env does
        nothing until a restart. Without this check the dashboard re-probes the
        old key forever and reports DOWN against a .env that is already fixed —
        telling you calls fail while hiding the one action that fixes them.
        """
        from slackqa.config import Settings

        try:
            on_disk = Settings().openrouter_api_key  # bypasses the settings cache
        except Exception:
            return False
        return bool(on_disk) and on_disk != self._bot.settings.openrouter_api_key

    async def snapshot(self) -> dict[str, Any]:
        return {
            "listener": self.listener(),
            "index": await self.index(),
            "api_key": await self.api_key(),
            "generated": _stamp(time.time()),
        }


# ----------------------------------------------------------------------- web


PROBE = web.AppKey("probe", StatusProbe)


async def _health(request: web.Request) -> web.Response:
    probe = request.app[PROBE]
    return web.json_response(await probe.snapshot())


async def _page(request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


def build_app(bot) -> web.Application:
    app = web.Application()
    app[PROBE] = StatusProbe(bot)
    app.router.add_get("/", _page)
    app.router.add_get("/health", _health)
    return app


async def start(bot, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(build_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("Status dashboard on http://%s:%d", host, port)
    return runner


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>slackqa status</title>
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1rem; background:#fff; color:#1a1a1a;
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width:44rem; margin:0 auto; }
h1 { font-size:1.35rem; margin:0 0 .2rem; }
.sub { color:#777; font-size:.85rem; margin:0 0 1.6rem; }
.card { border:1px solid #e3e3e3; border-radius:10px; padding:1rem 1.1rem;
  margin-bottom:.9rem; display:flex; gap:1rem; align-items:flex-start; }
.dot { width:12px; height:12px; border-radius:50%; margin-top:.4rem; flex:0 0 12px;
  background:#bbb; }
.dot.up { background:#22a75d; box-shadow:0 0 0 4px rgba(34,167,93,.15); }
.dot.down { background:#d94141; box-shadow:0 0 0 4px rgba(217,65,65,.15); }
.dot.warn { background:#d99b26; box-shadow:0 0 0 4px rgba(217,155,38,.15); }
.body { flex:1; min-width:0; }
.title { font-weight:650; display:flex; justify-content:space-between; gap:1rem; }
.verdict { font-weight:600; font-size:.85rem; }
.verdict.up { color:#1a8049; } .verdict.down { color:#c22f2f; }
.verdict.warn { color:#a8760f; }
.detail { color:#666; font-size:.87rem; margin-top:.3rem; }
table { width:100%; border-collapse:collapse; margin-top:.6rem; font-size:.85rem; }
th,td { text-align:left; padding:.3rem .5rem .3rem 0; }
th { color:#888; font-weight:600; font-size:.78rem; text-transform:uppercase;
  letter-spacing:.03em; }
td.num { font-variant-numeric:tabular-nums; }
.foot { color:#999; font-size:.78rem; margin-top:1.4rem; }
code { background:#f2f2f2; padding:.1rem .3rem; border-radius:3px; font-size:.85em; }
@media (prefers-color-scheme: dark) {
  body { background:#15171b; color:#e6e6e6; }
  .card { border-color:#2b2f36; }
  .sub,.detail,.foot { color:#98a0aa; } th { color:#7f8792; }
  .verdict.up { color:#5fce93; } .verdict.down { color:#f07a7a; }
  .verdict.warn { color:#e0b552; }
  code { background:#23262c; }
}
</style></head>
<body><div class="wrap">
<h1>slackqa status</h1>
<p class="sub" id="sub">loading…</p>
<div id="cards"></div>
<p class="foot">Polls <code>/health</code> every 10s. That endpoint returns the
same data as JSON if you want to script against it.</p>
</div>
<script>
function card(dot, title, verdict, detail, extra) {
  return '<div class="card"><div class="dot ' + dot + '"></div><div class="body">' +
    '<div class="title"><span>' + title + '</span>' +
    '<span class="verdict ' + dot + '">' + verdict + '</span></div>' +
    '<div class="detail">' + detail + '</div>' + (extra || '') + '</div></div>';
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
  });
}
function render(d) {
  var l = d.listener, i = d.index, k = d.api_key;
  var rows = i.channels.map(function (c) {
    return '<tr><td>#' + esc(c.channel) + '</td><td class="num">' + c.messages +
      '</td><td class="num">' + c.chunks + '</td><td>' + esc(c.newest_ago) + '</td></tr>';
  }).join('');
  var table = '<table><tr><th>channel</th><th>messages</th><th>chunks</th>' +
    '<th>newest</th></tr>' + rows + '</table>';

  document.getElementById('cards').innerHTML =
    card(l.ok ? 'up' : 'warn', 'Listener', l.ok ? 'up' : 'socket down',
         esc(l.state) + ' &middot; uptime ' + esc(l.uptime) +
         ' &middot; last Slack event ' + esc(l.last_event) +
         ' &middot; last question ' + esc(l.last_question)) +
    card(i.ok ? 'up' : 'down', 'Index last updated',
         i.ok ? esc(i.newest_ago) : 'empty',
         'newest indexed message ' + esc(i.newest) + ' &middot; last sync ' +
         esc(i.last_sync) + ' &middot; glossary ' + i.glossary_terms + ' terms (' +
         i.glossary_endorsed + ' endorsed)', table) +
    card(k.ok ? 'up' : (k.stale ? 'warn' : 'down'), 'API key',
         k.ok ? 'usable' : (k.stale ? 'restart needed' : 'rejected'),
         (k.stale ? '<b>A different key is in .env.</b> This process is still ' +
          'using the one it started with — restart to pick up the new one. ' : '') +
         esc(k.model) + ' &middot; ' + esc(k.detail) + ' &middot; checked ' +
         esc(k.checked));
  document.getElementById('sub').textContent = 'as of ' + d.generated;
}
function down(err) {
  document.getElementById('cards').innerHTML =
    card('down', 'Listener', 'unreachable',
         'No response from the status endpoint — the listener process is not ' +
         'running, or has stopped serving. Start it with ' +
         '<code>./slackqa run</code>.');
  document.getElementById('sub').textContent = 'last check failed: ' + err;
}
function tick() {
  fetch('/health', { cache: 'no-store' })
    .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(render)
    .catch(function (e) { down(e.message); });
}
tick();
setInterval(tick, 10000);
</script>
</body></html>
"""
