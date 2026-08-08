# slackqa

A Slack bot that answers questions from what people actually said in a channel,
with permalink citations — and says so when it doesn't know.

@mention it in a channel and it replies in a thread, grounded in that channel's
history. That's the whole product. Design decisions and their rationale are in
[SPEC.md](SPEC.md).

```
you    @slackqa what did we decide about the orders table?
bot    Move it to postgres, agreed in the 15 Nov thread — mysql row locks were
       causing contention under load and advisory locks would fix it. Bob said
       "let's do it next sprint". <permalink|2023-11-15>
```

## How it works

```
Slack events ──► ingest ──► chunker ──► embeddings ──► SQLite
                                                          │
question ──► retrieval (BM25 + vectors, RRF) ◄────────────┘
                     │
                     └──► answerer (Claude) ──► threaded reply with citations
```

- **Chunking.** A single Slack message is a poor retrieval unit — "+1", "see
  above" mean nothing alone. Threads become one chunk; unthreaded chatter is
  grouped into blocks split on a 10-minute gap.
- **Hybrid retrieval.** BM25 for exact tokens (ticket IDs, service names, error
  strings), dense vectors for paraphrase, fused with Reciprocal Rank Fusion.
- **Local embeddings.** fastembed/ONNX on CPU. Generation ships only retrieved
  chunks to OpenRouter; your archive never leaves the host.
- **Strict per-channel isolation.** A question in `#a` retrieves only from `#a`.
  There is no cross-channel code path, so there's no ACL to get wrong.
- **Refusal is a feature.** If the excerpts don't support an answer, the bot
  says so rather than answering from the model's general knowledge.

## Setup

### 1. Create the Slack app

At <https://api.slack.com/apps> → **Create New App** → From scratch.

**Do not enable public distribution.** Internal single-workspace apps get
50+ requests/minute and 1000 messages per call. Publicly distributed
non-Marketplace apps have been capped at 1 request/minute and 15 messages since
[29 May 2025](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps),
which makes backfill impractical.

- **Socket Mode** → enable, generate an app token (`xapp-…`) with `connections:write`
- **Event Subscriptions** → subscribe to `app_mention` and `message.channels`
  (add `message.groups` for private channels)
- **OAuth & Permissions** → bot scopes:
  `app_mentions:read`, `channels:history`, `channels:read`, `users:read`,
  `chat:write`, `reactions:write` (add `groups:history`, `groups:read` for
  private channels)
- Install to workspace, copy the bot token (`xoxb-…`)
- Invite the bot to each channel you want indexed

### 2. Configure

```bash
cp .env.example .env
```

```ini
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
OPENROUTER_API_KEY=sk-or-v1-...       # https://openrouter.ai/keys
MODEL=anthropic/claude-sonnet-5
CHANNELS="C0123ABC,C0456DEF"   # right-click channel → View channel details
```

Generation goes through [OpenRouter](https://openrouter.ai)'s OpenAI-compatible
API. `MODEL` takes any OpenRouter slug, so switching providers is a one-line
change — embeddings stay local either way, so only the retrieved chunks ever
leave the host.

Get channel IDs from the bottom of **View channel details** in Slack.

### 3. Run

```bash
uv sync && uv run slackqa run
```

On this machine, use the `./slackqa` wrapper instead — it pins the virtualenv
outside `~/Documents`, where the `.pth` problem below bites:

```bash
./slackqa sync     # backfill first
./slackqa ask C0123ABC "what did we decide?"
./slackqa run
```

First start backfills every configured channel and downloads the embedding
model (~130MB, once). Subsequent starts catch up and reconcile, then listen.

**Backfill takes a while and that's inherent.** Slack needs one
`conversations.replies` call per thread, and internal apps are paced at ~50
requests/minute — so a channel with ~500 threads takes ~10 minutes. Measured on
a real channel: 2,484 messages → 1,164 chunks in 10m16s. It's one-time; progress
is logged every 25 threads. Later starts only fetch what changed.

## Commands

| Command | Does |
|---|---|
| `slackqa run` | Sync, then listen for questions (default) |
| `slackqa sync` | Backfill / catch up / reconcile, then exit |
| `slackqa stats` | Show what's indexed per channel |
| `slackqa ask C0123ABC "what did we decide?"` | Ask from the terminal, no Slack round trip |

`slackqa ask` is the fastest way to check retrieval quality against your real
data without posting in a channel.

## Staying in sync

Slack does not replay events missed while the process was down, so live events
alone can't keep the index honest. On every start slackqa:

1. **catches up** — pulls everything since the last message it stored;
2. **reconciles** — diffs a trailing 30-day window against `conversations.history`
   and purges anything Slack no longer returns.

Step 2 is what makes deletion mean deletion. Someone pastes a credential,
deletes it, and rotates — without the diff, the bot would keep the secret and
could quote it back into the channel months later.

## Thread memory

Answers land in a thread, and a follow-up in that thread carries the earlier
turns — including the bot's own answers. So this works:

```
you   @LAIRbot when did we order the turbo pump?
bot   March 14th, from Pfeiffer. <permalink>
you   @LAIRbot no, that was the ion pump
bot   You're right — ...
```

Two things make that work. The thread is passed to the model as context (never
as citable evidence), and for short or anaphoric follow-ups the *search* query
is widened with the thread's earlier questions — "no, that was the ion pump"
retrieves nothing on its own. Self-contained questions are searched verbatim so
their own terms aren't diluted. Controlled by `THREAD_TURNS` (default 12).

Note this is deliberately narrower than the index: bot replies are excluded
from the corpus to prevent a self-citation loop, but within a single thread the
last answer is exactly what a correction refers to.

## Glossary

This group's own vocabulary — the knowledge that exists nowhere but this
channel. `data/glossary.md` is the source of truth; `data/glossary.html` is a
generated view.

Two kinds of entry, both channel-specific:

- **instrument** — a part or apparatus the group builds, buys, wires or operates
- **phenomenon** — an effect or measurement the group specifically studies

**Entries are scoped to one channel by default.** "Breakout box" names a
D25-to-BNC adapter in one channel and a differently-grounded box in another; a
single shared definition would be confidently wrong in one of them. Mining tags
each entry with the channel it came from, and a scoped entry is invisible
elsewhere. Delete the `channels:` line by hand to make an entry global — that
is how genuinely shared vocabulary is expressed. A channel-specific entry
shadows a global one of the same name.

Generic vocabulary is deliberately out of scope. "STM means scanning tunneling
microscope" tells the bot nothing it couldn't guess; the dimensions and wiring
of *this* group's breakout box exist only here. Terms judged out of scope are
recorded in `glossary-skip.txt` so they're never paid to re-evaluate — you can
also add a line there to veto a term permanently.

An entry carries a dense definition plus, when the channel says so, `status` and
`timeline`:

```markdown
## heat shield

Nested cryostat radiation shields at 4K, 20K, 77K and 220K stages, each a
cylindrical body with a draw-string actuated shutter operated via the wobble
stick; CryoVac M4x12 and M4x16 screws.

- kind: instrument
- status: All four shields installed; 77K shutter draw string no longer opens fully.
- timeline: 220K shield installed 2026-05-08; shutter fault unresolved.
- as-of: 2026-08-06
- aliases: JT heat shield, 4K shield, 77K shield
```

**Status and timeline are snapshots, not truth.** They're stamped with the date
they were derived, and the prompt tells the model to prefer fresher channel
excerpts and say when the glossary is behind — which it does:

> This is more recent than the glossary snapshot, so the glossary's "all shields
> installed" status is confirmed but the alignment fix is still outstanding.

Snapshots older than `GLOSSARY_REFRESH_DAYS` are re-derived on the next mining
pass. Endorsed entries are never rewritten — a person signed off on that text.

**Lookup is deterministic.** A term or alias appearing in a question injects its
definition and widens the search query. Plurals fold onto singulars, so "heat
shields" finds "heat shield" rather than creating a rival entry.

```bash
./slackqa glossary list
./slackqa glossary update                   # mine + refresh stale snapshots
./slackqa glossary html
./slackqa glossary endorse "heat shield" --by "Dong Chen"
./slackqa glossary add "breakout box" "A 12x6x4 box adapting two D25 connectors to 36 BNC cables" \
    --by "Dong Chen" --channel C0123ABC     # omit --channel to make it global
```

**Endorsement** marks an entry as human-checked. Mined entries are usable
immediately but flagged unendorsed, and the model says when it leaned on one.

**Mining** runs in the listener every `GLOSSARY_UPDATE_HOURS`, capped per pass.
A term must recur across several *separate* conversations — one thread repeating
its own topic isn't shared vocabulary. Candidates are frequent multi-word
phrases and acronyms; a single batched triage call classifies the shortlist
before any definition is paid for.

## Domain skill

`skills/answering/SKILL.md` carries lab-specific answering guidance and is
appended to the system prompt. It is a normal `SKILL.md` — frontmatter plus a
Markdown body — and only the body is sent; `name` and `description` exist for
humans and tooling.

Edit it and the change applies to the **next question**; the file is re-read
when its mtime changes, so there is no restart in the loop.

It deliberately contains only what the model cannot infer:

- **Instrument nicknames.** Beast, Tesla, Omi, Joel the Jeol, Createc read as
  ordinary English and are unguessable as hardware.
- **Channel-to-instrument binding.** In `#createc`, "the machine" means the
  Createc.
- **How to read lab chatter.** Dates supersede; negative results are results;
  copy part numbers and specs verbatim.
- **What each question type actually asks for**, drawn from the real query log.

Nothing in it repeats the base prompt. The test for adding a line is whether it
changes behaviour versus the default — grounding, citation and refusal rules are
already handled and would be pure token cost here.

One entry earned its place by measurement. "I remember we have X-ray
spectroscopy on the material" was refused even though the answer was indexed,
because the channel writes **XRD**, not "X-ray spectroscopy". The skill now maps
spoken technique names to their acronyms and tells the model to spend its one
refinement search on them; the same question now answers correctly.

## What isn't indexed

The corpus is what humans said to each other. Excluded: the bot's own replies
(indexing them creates a self-citation loop where wrong answers become citable
sources), the @mention questions themselves, third-party bots and webhooks, and
Slack system messages.

## Development

```bash
uv sync
uv run pytest          # 221 tests, no network, no model download
uv run ruff check .
```

Tests use a deterministic fake embedder, so the suite runs offline in under a
second.

### Known environment issue (this machine)

Something on this Mac re-applies the macOS `UF_HIDDEN` flag to files under
`.venv`, and CPython's `site.addpackage` **silently skips hidden `.pth` files**
— no error, no warning. That breaks editable installs of `src/` layout
projects: `import slackqa` fails even though `uv sync` reported success.

Tests are immune (`pythonpath = ["src"]` in `pyproject.toml`). For running the
CLI, either:

```bash
chflags -R nohidden .venv          # after any uv sync that rewrites the .pth
```

or set `PYTHONPATH=src`. A venv created outside `~/Documents` is unaffected, so
`UV_PROJECT_ENVIRONMENT` pointing elsewhere also works. Worth tracking down
which tool is setting the flag — it will affect every src-layout Python project
in that tree.

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `CHANNELS` | — | Comma-separated channel IDs. Quote the value — uv's `.env` parser rejects unquoted values containing spaces |
| `MODEL` | `anthropic/claude-sonnet-5` | Answer model (OpenRouter slug) |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Override for a proxy or gateway |
| `DATA_DIR` | `./data` | SQLite database location |
| `CHUNK_GAP_SECONDS` | `600` | Gap that starts a new window chunk |
| `TOP_K` | `8` | Chunks sent to the model |
| `SKILL_ENABLED` | `true` | Append the domain skill to the system prompt |
| `SKILL_PATH` | `skills/answering/SKILL.md` | Where that skill lives |
| `TEMPERATURE` | `0.0` | Sampling temperature; 0 keeps answers stable run to run |
| `MAX_ANSWER_TOKENS` | `2048` | Evidence-collection answers need the headroom |
| `CANDIDATES_PER_RETRIEVER` | `30` | Candidates each retriever contributes |
| `RRF_K` | `60` | Reciprocal Rank Fusion constant |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model |
| `RECONCILE_WINDOW_DAYS` | `30` | Deletion-diff lookback |
| `THREAD_TURNS` | `12` | Prior turns of a thread carried as context |
| `GLOSSARY_ENABLED` | `true` | Consult and mine the glossary |
| `GLOSSARY_UPDATE_HOURS` | `24` | How often the listener mines for new terms |
| `GLOSSARY_MAX_NEW_TERMS` | `5` | Cap on terms drafted per mining pass |
| `GLOSSARY_MIN_CONVERSATIONS` | `3` | Distinct conversations a term must span |
| `GLOSSARY_REFRESH_DAYS` | `7` | Age at which a status snapshot is re-derived |
| `GLOSSARY_MAX_REFRESH` | `5` | Cap on snapshots refreshed per pass |

## Limitations

- **Retrieval quality is unvalidated against real data.** There's no eval
  harness by design. Every question and the chunks retrieved for it are logged
  to the `query_log` table — that's the raw material for building a golden set
  later, from real questions rather than invented ones.
- **BM25 is weak on small corpora.** With few chunks there's little IDF signal,
  so a single incidental term match can outrank a semantically better chunk.
  `TOP_K=8` is the margin that absorbs this; it matters less as the index grows.
- **Mined definitions are unreviewed.** They're marked unendorsed and the model
  hedges on them, but nobody has checked them. Skim `glossary.html` and endorse
  the ones that are right.
- **Glossary status can lag.** It's a snapshot with a visible date, refreshed on
  a timer. The bot prefers fresher channel evidence, but between refreshes the
  HTML page can show a stale state.
- **No DM support.** A DM has no channel, so it would reintroduce the
  cross-channel ACL problem that per-channel isolation exists to avoid.
