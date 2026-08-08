# slackqa — Specification

A Slack bot that answers questions from what people actually said in a channel,
with citations, and says so when it doesn't know.

Status: draft, agreed 2026-08-05. Derived from a scoping session against the
`open-claude-tag` README, with most of that README's scope deliberately removed.

---

## 1. Goal

One sentence: **@mention the bot in a channel, get an answer grounded in that
channel's history, with permalinks to the source messages.**

Anything that doesn't serve that sentence is out of scope for v1.

## 2. Non-goals

Explicitly not building, despite the reference README specifying them:

| Dropped | Why |
|---|---|
| MCP tool integration | Not needed to answer questions from channel text. |
| `run_python` / code execution | Pure liability. The reference implementation's version was a trivially escapable fake sandbox. |
| Skill auto-creation | Solves a problem we don't have. |
| Ambient / heartbeat proactive posting | Unrequested behaviour; annoying by default. |
| Token budget enforcement | Premature at this volume. |
| Agentic memory curation (`MEMORY.md`) | The channel *is* the memory. |
| Admin UI | No. |
| Multi-provider LLM config | One provider, one model. |
| Cross-channel retrieval | Deliberately excluded — see §6. |

## 3. Corpus

**Backfill + live.** The bot answers from the channel's full history, not only
messages posted after installation.

- One-time backfill per channel via `conversations.history` +
  `conversations.replies`.
- Live ingest of new messages thereafter.
- Startup catch-up for anything missed while the process was down.

Scale target: 2–10 channels, tens of thousands of messages. SQLite throughout;
no external datastore.

### Rate limits

Verified 2026-08-05 against Slack's docs. The May 29 2025 restriction
(1 req/min, 15 messages/request) applies to **commercially distributed
non-Marketplace apps**. Internal customer-built apps installed in a single
workspace are exempt and retain **50+ req/min, up to 1000 messages/request**.

> **Constraint:** the Slack app must stay internal / single-workspace. Enabling
> public distribution silently drops backfill to ~15 messages/minute and makes
> this design unworkable.

Sources: <https://docs.slack.dev/apis/web-api/rate-limits>,
<https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps>

## 4. What counts as content

Indexed:

- Human-authored messages in the target channels.

Not indexed:

- The bot's own replies. Indexing them creates a self-citation loop: a wrong
  answer becomes a citable "source" and the error compounds on every related
  question. The reference implementation does exactly this.
- The `@mention` questions themselves — they are queries, not knowledge.
- Third-party bots and app webhooks (CI, Jira, alerting). In an active channel
  these dominate by volume and match everything while answering nothing.
- Slack system messages (join/leave/topic/purpose changes).

Detection: `bot_id` present, or `subtype` in the system-message set.

## 5. Retrieval unit (chunking)

Individual Slack messages are useless as retrieval units — "yeah that works",
"+1", "see above" carry no standalone meaning. Chunks are conversation-sized:

- **Threaded:** a thread root plus all its replies form one chunk.
- **Unthreaded:** contiguous messages in the same channel group into one chunk
  when the gap between consecutive messages is under **10 minutes**.
- A chunk carries: channel id, ordered participant list, start/end timestamps,
  the rendered text, and the `ts` of its first message (for permalinks).

Chunks are rebuilt for the affected window when a constituent message is edited
or deleted.

## 6. Retrieval scope — strict per-channel isolation

A question asked in `#A` retrieves **only** from `#A`.

This is a hard boundary, not a filter to be relaxed later. It matches the stated
goal literally, and it removes an entire class of bug: with no cross-channel
path there is no ACL to get wrong, nothing to audit, and no way for private
channel content to surface where it shouldn't.

## 7. Matching

**Hybrid, fused with Reciprocal Rank Fusion.**

- **BM25** via SQLite FTS5. Carries exact tokens Slack is full of: ticket IDs,
  error strings, service names, usernames.
- **Dense vectors** via brute-force cosine. Carries paraphrase — "how do we
  deploy" against a thread that says "shipping to prod".

At a few thousand chunks, cosine over an in-memory matrix is milliseconds, so no
vector database is required.

RRF: `score(c) = Σ 1/(k + rank_i(c))`, `k = 60`.

### Embeddings

Computed **locally** with `fastembed` (ONNX, ~50MB, no torch dependency).

The asymmetry matters: generation ships only the handful of retrieved chunks to
OpenRouter, but embedding would ship *every message ever indexed*.
Local embedding keeps the archive on the host, costs nothing per call, and makes
re-indexing an offline operation.

## 8. Answering

**Single-shot retrieve-then-answer, with at most one refinement round.**

1. Retrieve top-k chunks (hybrid, channel-scoped).
2. If no chunk clears the relevance threshold → refuse (see below).
3. Generate an answer grounded in the retrieved chunks.
4. If the model judges retrieval insufficient, it may reformulate the query
   **once** and retry. Hard cap: two retrievals per question.

Bounded so latency stays in the few-seconds range Slack users tolerate, and so
failures are attributable to either retrieval or generation rather than to an
unbounded loop.

Model: `anthropic/claude-sonnet-5`, reached through OpenRouter's
OpenAI-compatible API. Same model; the provider is an entry point, not a
change of behaviour. `MODEL` accepts any OpenRouter slug, so swapping is
configuration rather than code.

### Domain skill

`skills/answering/SKILL.md` is appended to the system prompt, carrying knowledge
the model cannot infer: instrument nicknames that read as ordinary English, the
binding from channel to instrument, conventions for reading experimental
chatter, and what each observed question type actually asks for. It is re-read
when its mtime changes so guidance can be tuned without a restart.

The admission test for a line is whether it changes behaviour versus the
default; anything the base prompt already covers is excluded as pure token cost.

Sampling is pinned to temperature 0. Retrieval was already deterministic, so
sampling was the sole source of run-to-run variation — and it was large enough
to flip a full evidence list into a refusal on identical input.

### Grounding

- Every claim cites a Slack permalink to its source message.
- Permalinks are constructed from channel id + message `ts`; no extra API call.
- **Refusal is required** when nothing clears the threshold. The bot says it
  couldn't find anything in the channel rather than answering from the model's
  general knowledge. Confidently inventing channel history that never happened
  is the worst available failure and the one that destroys trust fastest.

## 9. Edits and deletions

Both are honored. The index must not diverge from Slack.

- `message_changed` → re-chunk and re-embed the affected window.
- `message_deleted` → purge the message, re-chunk the affected window.
- **Offline deletions:** Slack does not replay events missed while the process
  was down. On startup, diff stored `ts` values for a trailing 30-day window
  against what `conversations.history` returns; anything stored but absent
  upstream was deleted and is purged.

Rationale: deletion is how someone remediates an accidentally-pasted credential.
An index that ignores it would keep the secret *and* let the bot quote it back
into the channel months later.

## 10. Interaction

- Ask by `@mention` in a channel.
- The bot replies **in a thread** off that message: keeps the channel quiet,
  keeps answers visible so colleagues can correct them, and unambiguously binds
  the question to its channel corpus.
- No DM support in v1 — a DM has no channel, so it would reintroduce the
  cross-channel ACL problem §6 exists to avoid.

## 10a. Thread memory

A mention inside a thread carries that thread's prior turns, including the
bot's own answers, so follow-ups and corrections resolve.

This is narrower than the index by design. §4 excludes bot replies from the
corpus to prevent a self-citation loop; thread memory is short-term context for
one exchange, passed as context and never citable as evidence.

Short or anaphoric questions additionally fold the thread's earlier human turns
into the *search* query — "no, that was the ion pump" retrieves nothing alone.
Self-contained questions are searched verbatim so their terms aren't diluted.

## 10b. Glossary

This group's own vocabulary, in `data/glossary.md`, rendered to
`data/glossary.html`. Markdown is the source of truth: an HTML source would
have to be machine-parsed and rewritten on every mined term, and one imperfect
hand-edit could silently break that parse.

Exactly two kinds of entry, both channel-specific — **instrument** (a part or
apparatus the group builds or operates) and **phenomenon** (an effect the group
studies). Generic vocabulary is out of scope: a dictionary expansion adds prompt
noise without adding knowledge. Rejected candidates are recorded in a skip list,
without which every pass would re-pay to triage the same terms forever.

- **Scoped per channel by default.** An entry records the channel it was mined
  from and is invisible to other channels; one with no `channels` field is
  global, and a scoped entry shadows a global one of the same name. This exists
  because "breakout box" was found to name different hardware in two channels,
  where a shared definition would have been confidently wrong in one of them.
- **Lookup is deterministic.** Terms and aliases match on word boundaries, with
  plural folding so "heat shields" resolves to "heat shield" rather than
  spawning a rival entry. Matches inject their definition and widen the search.
- **Provenance, not gatekeeping.** Mined entries are active immediately but
  marked unendorsed, and that state reaches the prompt so the model hedges.
- **Volatile fields are snapshots.** `status` and `timeline` are stamped with
  their derivation date; the prompt instructs the model to prefer fresher
  channel excerpts and to say when the glossary is behind. Snapshots older than
  `glossary_refresh_days` are re-derived. Endorsed entries are never rewritten.
- **Mining** is three stages in increasing cost: local candidate detection
  (multi-word phrases plus acronyms, spanning several distinct conversations),
  one batched triage call, then one definition call per surviving term.

## 11. Observability

No formal evaluation harness in v1.

Every question is logged with the chunks retrieved for it. Near-zero cost, and
it means a golden-set evaluation can later be built from real questions rather
than invented ones.

## 12. Deployment

Laptop for MVP; always-on local server thereafter.

Socket Mode: outbound websocket only, no public endpoint, no inbound firewall
rules. Startup catch-up (§9) is what makes an intermittent host viable — live
events are a latency optimisation, not the mechanism correctness depends on.

## 13. Slack app configuration

Bot token scopes:

| Scope | For |
|---|---|
| `app_mentions:read` | receive questions |
| `channels:history` | read + backfill public channels |
| `channels:read` | channel metadata |
| `users:read` | resolve display names |
| `chat:write` | post answers |
| `reactions:write` | ack indicator (optional) |
| `groups:history` | **only if** targeting private channels |

Socket Mode enabled; app-level token with `connections:write`.

Event subscriptions: `app_mention`, `message.channels`
(plus `message.groups` for private channels).

## 14. Open items

- Whether any target channel is private (adds `groups:read`/`groups:history`,
  and makes §6's isolation guarantee load-bearing rather than merely tidy).
