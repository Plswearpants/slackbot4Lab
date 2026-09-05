# Product log

A living record of this bot: what was built, what is next, and the decisions
that got us there — including the ones about privacy and how we treat other
people's systems.

**How this differs from the other docs.** [SPEC.md](SPEC.md) describes the
system as it stands now; [docs/failure-modes.md](docs/failure-modes.md) records
bugs and their fixes. This file is the *history and the pipeline*: why choices
were made, in what order, and what changed our minds.

**How to update it.** After each working session, add one entry at the top of
the session log. Sessions are newest-first so the current state is the first
thing you read. Each entry is:

- **When** — the date.
- **Abstract** — what this session was and why it was worth doing.
- **Details** — medium level: what changed, what broke, what we learned.
- **Decisions** — new ones get an ID below; existing ones are cited, not restated.

Decisions carry stable IDs (`D1`, `P1`) so entries can point at them. A
superseded decision is struck through and kept, never deleted — the fact that we
once thought otherwise is part of the record.

---

## Standing decisions — build philosophy

| ID | Decision | Since | Why |
|----|----------|-------|-----|
| **D1** | Answer only from the channel's own history, cite permalinks, and refuse when the channel doesn't support an answer. | 08-06 | A confident wrong answer about an instrument costs more than no answer. The citation is what makes the refusal credible. |
| **D2** | Strict per-channel isolation — no cross-channel code path exists, rather than a filter that could be bypassed. | 08-06 | A filter is a bug away from leaking. Absence of the path is the guarantee. |
| **D3** | Hybrid retrieval: BM25 + vectors fused with RRF. | 08-06 | Lexical alone misses paraphrase; vectors alone miss exact jargon (`PtSe2`, `LT-STM`). Lab language needs both. |
| **D4** | Chunk by conversation, not by token window — threads are units, unthreaded messages group on a 10-minute gap. | 08-06 | The answer to "why did the tip crash" is a five-message exchange. Splitting it mid-argument retrieves half a reason. |
| **D5** | Local embeddings, hosted answers. | 08-06 | Embedding 22k messages hosted is a recurring bill for something an ONNX model does on a laptop. Generation is where hosted quality actually matters. |
| **D6** | ~~Append-only; ignore edits and deletes.~~ **Superseded 08-06:** honour both, with real `DELETE`. | 08-06 | Decided during scoping, reversed the same session. A message someone deleted should not be quotable by the bot afterwards. See **P3**. |
| **D7** | OpenRouter as the single model gateway, model pinned. | 08-06 | One key, one place to swap models, no vendor SDK spread through the code. |
| **D8** | Tests encode failures we actually observed, not hypotheticals. | 08-07 | Nearly every test in `test_literature.py` is a real message shape from `#coolpapers` that broke something. |
| **D9** | Anything written to a shared surface is tagged and reversible. | 08-12 | `BOT_TAG` on every Zotero item means a bad run is one saved search away from being undone, not something a person spots by eye among their own references. |
| **D10** | No unrequested posting. The bot speaks when addressed. | 08-06 | A bot that talks unasked gets muted, and a muted bot answers nothing. Reconsidered 08-21 and upheld — see that entry for the canvas exception. |
| **D11** | Profiles and the glossary are different things and stay separate. | 08-19 | A glossary entry is a definition and is time-independent. A profile is a history and is time-dependent. Merging them makes both worse. |
| **D12** | Profiles are append-only with rolling resolution: one paragraph per past year, one or two sentences per month for the last six, recalibrated on update. | 08-19 | Recent detail is what a question usually needs; older detail compresses without losing the arc. Bounded size without a hard cutoff. |
| **D13** | Generation never overwrites human review. | 08-19 | An endorsed abstract is not regenerated; a reviewed timeline entry is never folded into its year. Condensation is otherwise safe because the source messages survive — but a person's judgement exists *only* in the profile, so folding it destroys it. |
| **D14** | Instrument profiles are seeded verbatim from the lab's published instrument pages. | 08-19 | The lab already wrote good descriptions. Paraphrasing them would add drift and subtract authority. |
| **D15** | Profile context reaches the model two ways: deterministic injection when a question names an entity, plus a tool call for open-ended questions. | 08-19 | Injection alone can't answer "who knows most about cryostats"; a tool alone means the model must think to ask. *Agreed; not yet built.* |

## Standing decisions — privacy and security

| ID | Rule | Since | Why |
|----|------|-------|-----|
| **P1** | Index humans only. Bot messages are never ingested. | 08-06 | Otherwise the bot cites itself and its own errors compound. |
| **P2** | Only channels the bot has been explicitly invited to. Being in the workspace grants nothing. | 08-06 | Invitation is the lab's consent, expressed in the tool they already use. |
| **P3** | Deletions are real deletions — row removed from messages, chunks, and the FTS index. | 08-06 | If someone deletes a message, no artefact of it should remain quotable. Supersedes **D6**. |
| **P4** | The repository stays **private**. | 08-06 | `SPEC.md` and `README.md` name the workspace URL, channel IDs, and lab specifics. |
| **P5** | Secrets live in `.env`, never committed. The API key is re-read at use, so rotating it needs no restart. | 08-06 | Reduces the temptation to hard-code a key "just to test". |
| **P6** | **No automated fetching through institutional subscriptions.** Metadata always; PDFs only where legally open (arXiv, Unpaywall); otherwise a link and a `needs-pdf` flag for a person to fetch with the Zotero Connector. | 08-12 | Publishers enforce systematic downloading against the institution's whole IP range. The cost of being caught falls on everyone at UBC, not on this bot. |
| **P7** | The dashboard is unauthenticated and localhost-only, and the page says so in a visible banner. | 08-19 | Deferring auth is a legitimate choice for internal validation. Looking protected while not being protected is not. |
| **P8** | Third-party APIs are used at their stated pace: arXiv 3 s, Crossref 0.2 s, a `User-Agent` carrying a contact address. | 08-12 | arXiv throttles silently — by returning no entry, which is indistinguishable from the paper not existing. We learned this by losing 86 papers to it. |
| **P9** | Answers name people; they do not `@`-mention them. | 08-21 | Answering someone's question should not generate a notification for a third party who didn't ask one. |

---

## What we want to do next

**Immediate**
- **Commit the outstanding work.** Nothing has been committed since `4c83484` (08-12): the user roster, mention resolution, chunk enrichment, schema migrations, literature pagination, the filed/seen fix, and the entire profile system.
- **Glossary migration** — move `status` / `timeline` / `as-of` out of glossary entries into instrument profile timelines and delete the `refresh_volatile` machinery. The only destructive step outstanding; do it as its own reversible commit.

**Next**
- **Deterministic profile injection** into the answering prompt when a question names a person or instrument (**D15**).
- **`search_profiles(query)` tool** for open-ended questions (**D15**).
- **Slack-signed links** for dashboard auth, replacing **P7**'s deferral — `@LAIRbot edit my profile` issues a signed URL.
- **The remaining 42 person profiles.** Three of 45 exist; 172 person-years of history are unwritten.

**Scoped, not started**
- **Filing receipts via reactions** — react 📚 when a shared paper reaches Zotero, ⚠️ when it needs a PDF. Needs no new scope and no reinstall. Makes an invisible pipeline visible.
- **Channel canvases** for instrument profiles and the glossary (see 08-21).
- **Onboarding DM** — a new channel member gets that instrument's profile and the glossary.

**Small and known**
- `ZnPc` / `ZnPC` are not case-deduplicated in mined systems lists.
- `HCl`, `PIC`, `PGYP`, `SQAO`, `S3W` still slip through the materials stoplist.
- 174 of 1,120 references remain unresolvable — bare URLs with no DOI. An RSC URL→DOI pattern would recover some.

---

## Sessions

### 2026-08-21 — Per-entry review, Slack action scoping, and this log

**Abstract.** Profile-level endorsement turned out to be the wrong granularity:
a ten-year timeline is not one claim, and a single button cannot say "2026 is
right, 2019 is wrong". Made every timeline entry independently endorsable and
editable. Then scoped what agentic actions the bot could take inside Slack, and
started this log.

**Details.**
- `Entry` gained `endorsed_by` and `edited_by`, round-tripping through the
  markdown as `- endorsed-by: Name (date)` beneath the entry text. Parsing had
  to learn that metadata lines *inside* an entry belong to that entry rather
  than to its body.
- Each entry renders its own state — `endorsed by Dong Chen`, `edited by
  Sarah`, or `unreviewed` — with inline endorse and edit forms. A name is
  required; there is no anonymous action, because an endorsement you cannot
  attribute tells you nothing.
- `POST .../entry/{period}/{endorse|edit}`, audited with the period, so
  `entry-edit / Sarah / 2019: text` is distinguishable from a change to another
  year.
- **A test caught a real loss.** Condensing folded an endorsed month into its
  year, silently discarding the endorsement. Fixed, and it clarified the
  principle now recorded as **D13**.
- Verified end to end against the live profiles; 344 tests, lint clean.
- Surveyed Slack agentic surfaces against current scopes
  (`app_mentions:read chat:write reactions:write channels:history channels:read
  users:read`). Everything beyond reactions needs new scopes and a reinstall.

**Decisions.** **D13** extended to per-entry review. **P9** recorded. **D10**
reconsidered and upheld, with a distinction worth keeping: we rejected proactive
*messages* because they interrupt. A canvas edit notifies nobody, so a canvas
that quietly stays current is a different kind of thing and remains on the table.

**Open question.** Adding any scope means reinstalling the app, and backfills
depend on the internal-app rate-limit exemption (1,000 messages per request
instead of 15). The exemption should follow from the app being internal and
non-distributed — but a 17-minute backfill becomes a multi-day one if that is
wrong, so verify against Slack's docs before reinstalling.

---

### 2026-08-19 — Entity profiles

**Abstract.** Retrieval answers "what was said", but not "who is this person"
or "what is this instrument for". Built a profile system so that context is
always available rather than reconstructed from whatever the search happened to
return. Designed by grilling first, which is what produced **D11** and **D12**.

**Details.**
- `profiles.py` (parse/render/condense), `profiling.py` (generation from
  chunks), `seeds.py` (instrument descriptions), `profileweb.py` (HTML views).
- Built five instrument profiles and three people — Jisun, Dong Chen, Fujia Li.
- Materials mining needed a stoplist and an alphabetic-stem check before it
  stopped treating `LAIR`, `BTW`, `DN40` and `TIC500` as sample systems.
- `condense()` initially removed *every* month of a folded year, so a January
  fold in August took July and August with it. Caught by a test before it
  touched real files.
- `#coolpapers` was given an instrument profile because it looked like one;
  instruments are now defined by having a seed.
- Endorse and edit at profile level, recorded in two places deliberately:
  markdown holds current state (what agents read, what a diff shows), SQLite
  holds the audit trail (what survives regeneration).

**Decisions.** **D11**, **D12**, **D13**, **D14**, **D15**, **P7**.

---

### 2026-08-12 – 08-18 — Making the corpus know who and what it contains

**Abstract.** Two gaps became obvious once real questions were asked: the bot
didn't know who anyone was, and `#coolpapers` — 1,417 messages of bare links —
was effectively unsearchable. Both are about the corpus describing itself.

**Details.**
- Per-channel user rosters; `<@U…>` resolved to display names in indexed text
  and in questions.
- Folded resolved paper metadata into chunk text: average chunk grew 214 → 513
  characters and the channel became searchable by what papers *say*.
- **A bug that poisoned the index.** `dict(AsyncSlackResponse)` raises
  `TypeError`; a bare `except` then cached raw user IDs as display names for all
  19 people, across 1,164 chunks. Fixed by using `.data` — and by never caching
  a failed lookup.
- `strip_mentions` was deleting people from questions: "what is @Markus working
  on" became "what is working on". Reported as "the listener seemed to be having
  problem".
- Neither literature pass paginated: scan saw 400 messages and resolve saw
  1,000 of 1,417. References went 795 → 1,120 once fixed.
- `seen_reference` vs `filed_reference` — resolving records a row too, so the
  filing pass was skipping 663 papers it had never filed.
- arXiv throttling cost 86 papers before **P8** existed.

**Decisions.** **P8**. **D8** reinforced — every one of these is now a test.

---

### 2026-08-12 — Zotero filing and deep query expansion

**Abstract.** Papers shared in Slack were reaching nobody's library. Filing them
into a Zotero group collection per channel turns a scroll-back into a catalogue.
Separately, made query expansion opt-in rather than always-on.

**Details.**
- Three-tier filing: metadata always (Crossref/arXiv), PDF where legally open
  (arXiv, Unpaywall), otherwise a link plus `needs-pdf`.
- Reader tags from `@`-mentions: naming a colleague beside a link means "read
  this", and that survives into the library as `for:Name`.
- Zotero's 4-step upload, and its `mtime` in **milliseconds** — a missing or
  zero value is rejected with "File modification time not provided", a message
  that names the field but not the unit.
- The arXiv Atom feed opens with `<title>ArXiv Query: …</title>`, so every
  preprint was briefly titled that. Scoping to `<entry>` fixed it.
- No explicit timeouts meant aiohttp's 5-minute default per request: two papers
  took 26 minutes. With timeouts, ten took 2.5.
- Query expansion behind a flag — always-on expansion changes what every
  question retrieves, which is not something to enable invisibly.

**Decisions.** **D9**, **P6**.

---

### 2026-08-07 — Dashboard and retrieval evals

**Abstract.** No way to tell whether the bot was alive, current, or authorised
without reading logs. Three indicators answer that; an offline eval harness
answers whether retrieval still works after a change.

**Details.**
- Listener up, index last updated, API key usable — on a local port.
- `is_connected()` is a coroutine; the unawaited call was always truthy, so the
  listener indicator could never go red. The fake client was synchronous, which
  is what hid it. Fixed, with a regression test.
- **The first eval tested a pipeline production doesn't run** — it called
  `Retriever.retrieve()` directly, bypassing glossary expansion. Fixed by
  extracting a shared `build_search_query()` so eval and production take the
  same path.

**Decisions.** **D8**.

---

### 2026-08-06 — Scoping, spec, and first working bot

**Abstract.** Started from a reference implementation whose README promised far
more than it delivered. Rather than inherit that scope, ran a grilling session
to decide what this lab actually needs, wrote it down, then built it.

**Details.**
- Scope settled: backfill plus live, a few channels with real history, threads
  and time-windows, hybrid retrieval, strict per-channel isolation, permalink
  citations with refusal, `@`-mention interaction, humans only, laptop-hosted.
- Dropped from the reference design: MCP tools, arbitrary code execution,
  ambient monitoring. All three were capability without a use case here.
- `ts` kept as TEXT verbatim for permalinks, with a separate `ts_num` REAL for
  ordering — Slack timestamps are identifiers, not numbers.
- `oldest="0.000000"` is rejected by Slack as `invalid_ts_oldest`; the parameter
  has to be omitted. The fake client defaulted it to `"0"`, which hid the bug.
- pydantic-settings JSON-decodes complex types at source level, so `CHANNELS`
  was unparseable until annotated with `NoDecode`.

**Decisions.** **D1**–**D7**, **D10**, **P1**–**P5**.
