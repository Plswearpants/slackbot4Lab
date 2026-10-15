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
| **D2** | Strict per-channel isolation — no cross-channel code path exists, rather than a filter that could be bypassed. | 08-06 | A filter is a bug away from leaking. Absence of the path is the guarantee. **Deliberate exception, 10-15:** person and instrument profiles are built from every indexed channel and may accompany an answer in any of them. They are background, never cited (rule 9), and all indexed channels are lab-internal. |
| **D3** | Hybrid retrieval: BM25 + vectors fused with RRF. | 08-06 | Lexical alone misses paraphrase; vectors alone miss exact jargon (`PtSe2`, `LT-STM`). Lab language needs both. |
| **D4** | Chunk by conversation, not by token window — threads are units, unthreaded messages group on a 10-minute gap. | 08-06 | The answer to "why did the tip crash" is a five-message exchange. Splitting it mid-argument retrieves half a reason. |
| **D5** | Local embeddings, ~~hosted answers~~. **Superseded 09-01:** see **D16**. | 08-06 | Embedding 22k messages hosted is a recurring bill for something an ONNX model does on a laptop. The second half held only until the lab had its own GPUs. |
| **D6** | ~~Append-only; ignore edits and deletes.~~ **Superseded 08-06:** honour both, with real `DELETE`. | 08-06 | Decided during scoping, reversed the same session. A message someone deleted should not be quotable by the bot afterwards. See **P3**. |
| **D7** | OpenRouter as the single model gateway, model pinned. | 08-06 | One key, one place to swap models, no vendor SDK spread through the code. |
| **D8** | Tests encode failures we actually observed, not hypotheticals. | 08-07 | Nearly every test in `test_literature.py` is a real message shape from `#coolpapers` that broke something. |
| **D9** | Anything written to a shared surface is tagged and reversible. | 08-12 | `BOT_TAG` on every Zotero item means a bad run is one saved search away from being undone, not something a person spots by eye among their own references. |
| **D10** | No unrequested posting. The bot speaks when addressed. | 08-06 | A bot that talks unasked gets muted, and a muted bot answers nothing. Reconsidered 08-21 and upheld — see that entry for the canvas exception. |
| **D11** | Profiles and the glossary are different things and stay separate. | 08-19 | A glossary entry is a definition and is time-independent. A profile is a history and is time-dependent. Merging them makes both worse. |
| **D12** | Profiles are append-only with rolling resolution: one paragraph per past year, one or two sentences per month for the last six, recalibrated on update. | 08-19 | Recent detail is what a question usually needs; older detail compresses without losing the arc. Bounded size without a hard cutoff. |
| **D13** | Generation never overwrites human review. | 08-19 | An endorsed abstract is not regenerated; a reviewed timeline entry is never folded into its year. Condensation is otherwise safe because the source messages survive — but a person's judgement exists *only* in the profile, so folding it destroys it. |
| **D14** | Instrument profiles are seeded verbatim from the lab's published instrument pages. | 08-19 | The lab already wrote good descriptions. Paraphrasing them would add drift and subtract authority. |
| **D15** | Profile context reaches the model two ways: deterministic injection when a question names an entity, plus a tool call for open-ended questions. | 08-19 | Injection alone can't answer "who knows most about cryostats"; a tool alone means the model must think to ask. **Injection shipped 09-01; `search_profiles` still open.** |

| **D16** | Answers come from the lab's own GPU cluster first, with a hosted model as fallback. A refusal is an answer and is never retried elsewhere. | 09-01 | Supersedes half of **D5**: once the lab has its own hardware, generation need not leave it. Retrying against a second model until one is willing to speak would demolish **D1**, so only a failure — unreachable, timeout, error, empty — triggers the fallback. |
| **D17** | Ambiguity is narrowed in code first, and only what the channel cannot settle is handed to the model. | 09-01 | Extends **D2**'s reasoning to names: if one Alex appears in the retrieved excerpts, that is the Alex meant, deterministically and for free. The model decides only what evidence cannot, and says a name is ambiguous rather than guessing. |
| **D19** | The model drives retrieval through a `search_channel` tool. The harness supplies only what the model cannot know — today's date, the channel roster, and the channel itself — and executes the searches it asks for. | 10-13 | Temporal parsing ("last month", "since September") and person extraction are things the model already does well; writing regex for them would be harness standing in for capability. The tool has no channel parameter, so **D2** stays absolute. First turn must search; a 60 s wall-clock budget, not a round count, ends the loop. Born from Yuan's Oct 6 update being out-ranked by a 2025 conversation and a password note. |
| **D18** | Domain knowledge that differs between labs is configuration, not code. | 09-28 | `skills/answering/SKILL.md` is already a path; `seeds.py` is not. The distinction only matters if another lab ever runs this, but it is cheap to respect now. |

## Standing decisions — privacy and security

| ID | Rule | Since | Why |
|----|------|-------|-----|
| **P1** | Index humans only. Bot messages are never ingested. | 08-06 | Otherwise the bot cites itself and its own errors compound. |
| **P2** | Only channels the bot has been explicitly invited to. Being in the workspace grants nothing. | 08-06 | Invitation is the lab's consent, expressed in the tool they already use. |
| **P3** | Deletions are real deletions — row removed from messages, chunks, and the FTS index. | 08-06 | If someone deletes a message, no artefact of it should remain quotable. Supersedes **D6**. |
| **P12** | **Provenance:** the index mirrors Slack as it reads today. An edited message is indexed in its edited form; a message deleted in Slack leaves the index however old it is. | 10-15 | Everything indexed must be something a citation can point back to — a permalink should open on exactly what the bot quoted. `message.channels` had never been subscribed, so edits were never picked up and deletions older than the 30-day startup reconcile were never seen; `slackqa reconcile` repairs the whole history in two phases (plan live, apply with the listener stopped). |
| **P4** | The repository stays **private**. | 08-06 | `SPEC.md` and `README.md` name the workspace URL, channel IDs, and lab specifics. |
| **P5** | Secrets live in `.env`, never committed. The API key is re-read at use, so rotating it needs no restart. | 08-06 | Reduces the temptation to hard-code a key "just to test". |
| **P6** | **No automated fetching through institutional subscriptions.** Metadata always; PDFs only where legally open (arXiv, Unpaywall); otherwise a link and a `needs-pdf` flag for a person to fetch with the Zotero Connector. | 08-12 | Publishers enforce systematic downloading against the institution's whole IP range. The cost of being caught falls on everyone at UBC, not on this bot. |
| **P7** | The dashboard is unauthenticated and localhost-only, and the page says so in a visible banner. | 08-19 | Deferring auth is a legitimate choice for internal validation. Looking protected while not being protected is not. |
| **P8** | Third-party APIs are used at their stated pace: arXiv 3 s, Crossref 0.2 s, a `User-Agent` carrying a contact address. | 08-12 | arXiv throttles silently — by returning no entry, which is indistinguishable from the paper not existing. We learned this by losing 86 papers to it. |
| **P9** | Answers name people; they do not `@`-mention them. | 08-21 | Answering someone's question should not generate a notification for a third party who didn't ask one. |
| **P10** | While `LOCAL_ONLY` is false, channel content leaves the lab whenever the cluster is unreachable — silently, with nothing visible in Slack. The dashboard reports which backend served and counts `N/M answered off-site`. | 09-01 | "Nothing leaves the lab" is currently a tendency, not a promise. That is a defensible choice, but it must be a stated one: do not tell colleagues otherwise while this is false. |
| **P11** | Never `source .env`. Exported variables outrank the file, so a stale export silently wins and also defeats the credential hot reload. Every command warns when one disagrees with the file. | 09-01 | Cost ten minutes of debugging HTTP 405s against a URL that had already been fixed on disk. Secrets are named in the warning, never printed. |

---

## What we want to do next

**Immediate**
- **Off-machine backups — monthly, by hand.** Nightly backups run on the
  institute machine and a restore is verified. The off-machine copy is a
  monthly `rsync` run in person, since the Secure Enclave key needs Touch ID.
- **Redact credentials from the index.** A password posted in `#omicron` is
  retrievable and was quoted back verbatim in answers. Rotate it; then scrub
  high-confidence credential shapes at ingest and from existing chunks.
- **Retire the laptop copy** — but only after the service survives a reboot
  unaided and the off-machine copy exists. It is 99 messages stale, so rolling
  back now costs a `slackqa sync`.
- **Glossary migration** — move `status` / `timeline` / `as-of` out of glossary
  entries into instrument profile timelines and delete `refresh_volatile`. The
  only destructive step outstanding; its own reversible commit.

**Next**
- **`search_profiles(query)` tool** — the open half of **D15**.
- **The remaining 42 person profiles.** Three of 45 exist.
- **Slack-issued signed links** for dashboard auth, replacing **P7**'s
  deferral. Needs the dashboard to be reachable at all first (see below).

**Decisions waiting on us, not on code**
- **`LOCAL_ONLY`** — whether **P10** becomes a promise. On campus the cluster is
  directly reachable, so the fallback is now a genuine outage signal rather
  than a daily event. This is the moment to decide.
- **Service account vs personal login** — `.env` on the institute machine holds
  the Slack, Zotero and OpenRouter credentials under a personal account.
- **Other labs.** Groups outside LAIR have asked to use this. Two shapes:
  run instances for them, or give them the software and the runbook. The code
  is nearly ready for either — almost everything is already per-directory
  configuration, and `env_file=".env"` resolves relative to the working
  directory, so one `systemd` template and one directory per lab would do it.
  The open question is custody, not engineering: hosting means holding another
  group's Slack history and bot token, and appearing in your backups. If we do
  host, it is one process per lab — **D2**'s reasoning says isolation should be
  the absence of a code path, not a filter. Also verify first whether a
  distributed Slack app forfeits the internal-app rate-limit exemption; if it
  does, each lab must create their own app.
- **Dashboard reachability** — loopback plus an SSH tunnel works for one person
  over VPN. The UBC SSH Proxy hostname is still unknown, and falcon cannot
  forward. Do not expose the port as a shortcut (**P7**).

**Small and known**
- `ZnPc` / `ZnPC` are not case-deduplicated in mined systems lists.
- `HCl`, `PIC`, `PGYP`, `SQAO`, `S3W` still slip through the materials stoplist.
- 174 of 1,120 references remain unresolvable — bare URLs with no DOI. An RSC
  URL→DOI pattern would recover some.
- `seeds.py` is the last LAIR-specific thing baked into code (**D18**).

## Sessions

### 2026-10-13 – 10-15 — The bot searches for itself; ingestion turns out never to have worked

**Abstract.** With the bot always-on, two problems surfaced that restarts had
been hiding. Retrieval had no sense of time, so "what is Yuan doing this month"
missed his main update; the fix was to let the model drive its own searches
(**D19**). And the bot had never been subscribed to plain channel messages —
only to @mentions — so live indexing, edits and most deletions had never
worked. Repairing that history led to **P12**, the provenance rule.

**Details.**
- `LOCAL_ONLY=true` is now on. An unreachable cluster at startup first killed
  the service into a restart loop; it now stays up, says so loudly, and
  refuses questions with a truthful Slack reply.
- A transient empty completion from the cluster cost a real answer under
  `LOCAL_ONLY`, so local calls now retry once.
- **D19**: one `search_channel(query, person, since, until)` tool, no channel
  parameter, first turn must search, 60 s wall-clock budget. Probing showed
  both cluster models extract the person and turn "last month" into exact
  dates — but only if told today's date; without it one model invented 2025.
  A round cap of 4 then cut a real investigation short at 5 s, and the model's
  narration ("Let me try a few more searches…") was posted as the answer. The
  cap is now a runaway guard (12) and the forced final turn says why.
- `message.channels` had never been subscribed. Found because an always-on
  service is the first deployment nothing restarts. Your own "Test thread for
  LAIRbot-remote" was the proof it wasn't arriving. Fixed in the Slack app
  config; verified with a plain message.
- **P12** and the full reconcile: plan with the bot live (read-only, ~1 h),
  apply with it stopped, re-checking each change against Slack at the moment
  of writing. Two bugs fixed on the way: the 30-day reconcile left deleted
  text in chunks between its first and last deletion, and live rebuilds
  stripped #coolpapers' paper titles. Planning was then rate-limited within a
  minute — it lacked backfill's pause between threads — and 429s were
  consuming the retry budget.
- **D2** profile exception recorded as deliberate.
- The planner's completion is emailed from the server through UBC's relay.
  The first attempt vanished: its sender was the machine's own address. Sent
  from the user's address, it arrived.

**Lessons worth keeping.**
- A working question path proves nothing about ingestion. `app_mention` and
  `message.channels` are separate subscriptions.
- `pkill -f` over SSH can match the remote shell running it, because the whole
  script is that shell's command line. Three "Touch ID failures" were this.

**Open.**
- **Apply the reconcile** once the plan's counts have been reviewed.
- **Credential redaction** — the `#omicron` password is still retrievable.
- The startup line `key accepted (model=anthropic/claude-sonnet-5)` is
  misleading under `LOCAL_ONLY`.

### 2026-09-01 – 10-13 — Profiles reach the model; the bot leaves the laptop

**Abstract.** Two arcs. First, the profile system stopped being a read-only
knowledge base and started reaching the answering prompt. Second, generation
moved onto the lab's own GPUs and the whole service moved onto an always-on
machine — which took two attempts, because the first one's CPU was from 2008.

**Details — profiles now inform answers.**
- Deterministic injection: a question naming a person or instrument gets that
  profile ahead of the excerpts. Rule 9 in the system prompt places profiles on
  the *interpretation* side, like the glossary — they say who is meant, they
  never become claims, and they are never citable. That had to be explicit,
  because rule 1 forbids outside knowledge about people and a profile is
  exactly that.
- Ambiguity is narrowed in code before the model sees it (**D17**). Two guards
  were needed: a first name must be capitalised as written, *and* not be an
  ordinary English word. `Will Ho` against "will the tip crash" was the
  motivating case; the stoplist was briefly broken by comparing `"Will"` to a
  lowercase set, so only the sentence-start capitalisation leaked.
- Candidate groups are labelled mutually exclusive and carry abstracts only.
  Unlabelled, a model reads three profiles for one name as three relevant
  people and blends them into a composite who does not exist.
- `Alex` → `Alexander` is not inferred. Guessing that `Jo` means John or Joan
  is the kind of confident wrong answer this bot exists to avoid, so a nickname
  is a declared `aliases:` entry — and then it *creates* ambiguity rather than
  bypassing it.
- Per-entry endorsement and editing, which produced **D13**'s extension: a
  reviewed timeline entry is never folded into its year. Condensation is safe
  only because the source messages survive; a person's judgement exists only in
  the profile.

**Details — local models.**
- `LocalCompleter` plus `FallbackCompleter` (**D16**). GLM and Qwen both held
  the citation and refusal discipline in testing; notably Qwen volunteered that
  2023 material did not answer a question about *recent* issues, which is
  closer to **D1**'s spirit than the confident answer GLM gave.
- The dashboard names the backend that *served*, and counts `N/M answered
  off-site` (**P10**). A snapshot of the last answer hid whole mornings.
- `.env` shadowing guard (**P11**), after `set -a; source .env` left a stale
  URL outranking the corrected file and produced ten minutes of HTTP 405s.
- An **Indexed data** card: per-channel messages, chunks, size and backfill
  state. It also separated "last synced" from "newest message" — a quiet
  channel reading "8d ago" looked broken. Along the way, the old builder was
  materialising all 22,683 `Message` objects on every 10-second poll just to
  call `len()`; now three aggregate queries per channel.

**Details — the migration.**
- `qmi-463` turned out to be a 2008 Xeon E5462: SSE4.1, no SSE4.2. numpy 2.x
  and `onnxruntime` both refuse to run, so `fastembed` and half of **D3** were
  dead and the test suite could not even be collected. Not fixable — bare
  metal, no CPU model to change.
- `qmi-nf-cr` (Xeon E-2124G, 2018, Ubuntu 24.04, ext4 on local RAID) works:
  383 tests pass *on the target*, the check that was impossible before.
- Access is **port 7743** over the PHAS VPN — no gateway, despite the advice.
  `ssh.phas.ubc.ca` accepts CWL but has TCP forwarding disabled, so it cannot
  be a jump host.
- The cluster had renamed its models; `.env` asked for one that no longer
  existed, got HTTP 400, and answered everything via OpenRouter. Then GLM-5.3
  turned out to be a reasoning model whose `reasoning_content` is billed
  against the same `max_tokens`, so a RAG-sized prompt spent the whole budget
  thinking and returned an empty completion. `MAX_ANSWER_TOKENS=6144`.
- Data verified by table, not by assumption: `literature` 1,120, `users` 64,
  `profile_audit` 5 all identical; `messages` ahead by 99 because the service
  caught up after cutover. A restore from the nightly backup was proven, not
  assumed.
- Three of those four failures were **invisible from Slack** — answers kept
  arriving, cited and plausible, from the wrong provider. Recorded in
  [docs/deployment.md](docs/deployment.md) §12.

**Decisions.** **D16**, **D17**, **D18**, **P10**, **P11**. **D5** half
superseded; **D15** half shipped; **D13** extended to entries. **D10** upheld
after reconsidering canvases — a canvas edit notifies nobody, so it remains on
the table where proactive messages do not.

**Left open deliberately.** `LOCAL_ONLY`, the service-account question, and
whether to host other labs. All three are custody decisions rather than
engineering ones, and all three are now listed above rather than drifting.

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
