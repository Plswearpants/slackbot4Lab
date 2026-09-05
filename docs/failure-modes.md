# Failure modes of the prior art

Status: draft, 2026-08-21. Written while scoping the expansion from
"channel Q&A" to "research workspace" — shared library and annotations,
multi-source ingest, one retrieval entrance.

Organisational Q&A bots are not new and most of them are bad. This is a study
of the documented failures, sorted by how directly each one threatens what we
are about to build. Every case gets the same three lines: what happened, the
mechanism underneath it, and what it costs us.

The short version: v1's safety is not the model. It comes from three invariants
— **one corpus type**, **strict per-channel isolation**, **refusal by default**
— and each proposed expansion erodes a different one.

---

## 1. Authoritative fabrication in a literature context

**Meta Galactica** (Nov 2022) was trained on 48M papers, offered as a tool to
suggest citations and discover related work, and withdrawn three days after
launch. It generated references to papers that do not exist, sometimes
attributing them to real authors, in fluent scientific register.

**Mechanism.** Citation strings are cheap to generate and expensive to verify.
A fabricated reference is well-formed by construction — it has an author, a
year, a plausible title — so the reader's usual malformedness cue never fires.
The domain where the output looks most authoritative is exactly the domain
where the failure is least detectable.

**For us.** This is the single most relevant case in the file, because our
literature layer is the part of the system that most resembles Galactica's
pitch. We already have the right defence and it is worth naming as a rule
rather than leaving it as an implementation detail:

> **Citations are resolved, never generated.** A reference reaches a reader
> only by having survived a lookup against Crossref, arXiv or Unpaywall from an
> identifier found in the source text. The model may say a paper was discussed;
> it may not compose the paper's identity.

`literature.py` works this way today — extract identifier, resolve, and where
resolution fails, tag `needs-pdf` and leave a gap a person can see. Preserve
that under any summarisation feature: the moment a "related work" or "papers on
this" feature composes a citation instead of retrieving one, we are Galactica
with a smaller audience.

Corollary for annotation-grounded answers: an annotation's anchor (which paper,
which locator) is provenance and must be carried, not paraphrased.

## 2. The organisation is bound by what the bot said

**Moffatt v. Air Canada** (2024 BCCRT 149). Air Canada's chatbot told a
bereaved customer he could claim a bereavement fare retroactively; the actual
policy required applying before travel. Air Canada argued the chatbot was "a
separate legal entity responsible for its own actions". The tribunal rejected
this outright — the airline is responsible for all information on its site,
static page or chatbot alike — and awarded damages.

**Mechanism.** Deploying an answering system publishes its answers as the
organisation's position. There is no "the AI said it" disclaimer layer between
the two.

**For us.** No legal exposure in a lab, but the same structure with a worse
outcome: an answer about a bake-out procedure, a tip-conditioning step, or what
pressure the chamber should reach becomes the de facto protocol, because it is
faster to ask than to find the person who knows. Two things follow.

- Answering in-thread (§10) is load-bearing, not cosmetic. It is the only
  reason a colleague can see a wrong answer and correct it in place.
- **We currently drop the corrections.** §4 excludes bot replies from the index
  to avoid a self-citation loop, which is right, but it means an in-thread
  correction — "no, that's the ion pump, not the turbo" — gets indexed with its
  antecedent missing. The record keeps the rebuttal and forgets the claim, and
  a future retrieval can surface the correction as a standalone assertion.
  Thread memory (§10a) holds the pair for one exchange and then loses it. This
  is an open defect, not a design choice; see Open questions.

## 3. Vocabulary that does not transfer between contexts

**IBM Watson for Oncology / MD Anderson.** Trained on hypothetical cases
authored at Memorial Sloan Kettering, deployed at MD Anderson against different
vocabulary and different practice. It produced recommendations internal
documents called "unsafe and incorrect" — in the best-known example, a
chemotherapy regimen including bevacizumab for a lung cancer patient with
severe active haemorrhage. MD Anderson shelved the project after roughly $62M
and no patients treated.

**Mechanism.** A term's meaning is local to the group that uses it. A system
that assumes one definition across contexts is confidently wrong in every
context but the one it learned from, and confidence is uniform across both.

**For us.** We already made the correct call here for one reason and should
notice how general it is. The glossary is scoped per channel (§10b) because
"breakout box" was found to name different hardware in two channels. That same
argument applies verbatim to everything we are about to add:

- **Profiles** must not merge a person's or instrument's meaning across
  contexts where the surrounding practice differs.
- **The library** is the exception that proves it: a paper's identity *is*
  global, which is why one Zotero collection per channel is about who cares,
  not about what the thing means.
- Any future cross-source retrieval inherits this. Sources do not share a
  vocabulary just because they share an entrance.

## 4. Frictionless retrieval turns latent exposure into actual exposure

**Microsoft 365 Copilot oversharing.** Copilot grounds answers in content the
user can already open. It adds no access. What it removes is the friction that
made years of accumulated permission sprawl — broad sharing links, inherited
folder permissions, "everyone except external users" sites — practically
undiscoverable. Consultancies auditing rollouts report material exposure in the
large majority of tenants; Gartner found oversharing concerns delayed 40% of
rollouts by three months or more.

**Slack AI indirect prompt injection** (PromptArmor, Aug 2024) is the sharper
version. A malicious instruction posted in a *public* channel entered the RAG
index; when a victim queried Slack AI, the instruction was retrieved and
followed, rendering a markdown link that carried private-channel content — an
API key — to an attacker's server in the query string. Slack's first response
was that public messages are searchable by design. They patched it.

**Mechanism.** An LLM cannot separate instructions from retrieved content. Once
retrieval spans a trust boundary, retrieved text is an instruction channel from
whoever wrote it to whoever queries.

**For us.** §6 — strict per-channel isolation, "no cross-channel code path, so
no ACL to get wrong" — is the invariant that makes both of these inapplicable
today. **The single-entrance-point idea deletes it.** That is not a reason not
to build it, but it converts a structural guarantee into a permissions system
we would have to get right and keep right, and it makes an entire attack class
live for the first time.

Two specifics if we go there:

- **Attacker-controlled text enters the corpus.** Paper PDFs, publisher
  metadata and other people's annotations are text we did not author. An
  injection in a PDF's body is a retrieved instruction. Today this cannot reach
  anything because literature handling never feeds a channel answer; unified
  retrieval joins them.
- **The rendering surface is the exfiltration surface.** The Slack AI attack
  worked through link rendering, not through the answer text. If answers ever
  render model-authored URLs, that is the hole.

## 5. Disclaimers are not remediation

**NYC MyCity.** The city's official business chatbot was documented by The
Markup (Mar 2024) telling employers they could take workers' tips, landlords
they could refuse housing vouchers, and businesses they could go cash-free —
each of which is illegal in New York. The mayor declined to take it down,
calling AI a once-in-a-generation opportunity; the city added a disclaimer
telling users not to treat the answers as professional advice. It was
eventually removed anyway.

**Mechanism.** A disclaimer transfers blame without changing behaviour. Users
who trust the interface do not read it, and users who read it lose the reason
to use the tool at all — so it costs adoption *and* prevents nothing.

**For us.** The relevant instinct is already in the spec: refusal is required
when nothing clears the threshold, and unendorsed glossary entries reach the
prompt so the model hedges. Keep the pattern — **calibrate the answer, don't
caveat the product.** If a class of question is answered wrong, the fix is to
refuse that class, not to add a line about how the bot can make mistakes.

## 6. The likely failure is not dramatic — it is nobody using it

**MIT NANDA, *The GenAI Divide* (2025).** Of 300 analysed enterprise
deployments plus interviews and surveys, ~95% of generative AI pilots produced
no measurable return. The report attributes this to a "learning gap" — an
organisational failure to fit the tool into how work is actually done — rather
than to model capability.

**Mechanism.** Systems get built against an imagined workflow. The imagined
user asks broad synthesis questions; the real one wants to know what pressure
the prep chamber was at last Tuesday, and stops asking after two answers that
missed.

**For us.** This is the most probable way this project fails, and the cheapest
to defend against. §11 already logs every question with the chunks retrieved
for it. The lesson is to promote that log from observability to **the primary
input for what we build next**: the distribution of real questions decides
whether profiles, or the library, or something not on this list, is the next
thing worth building. A feature that no logged question would have needed is a
feature built for the imagined user.

---

## What the expansions cost

| Expansion | Invariant it erodes | Consequence |
|---|---|---|
| Group library + shared annotations | one corpus type | Sources of unequal authority (published paper, colleague's marginal note, model summary) become interchangeable in retrieval unless authority is carried as metadata. Fabrication risk moves to its most dangerous domain (§1). |
| Multi-source ingest | one corpus type | Non-Slack text is not authored by us and is not permission-checked by Slack. Provenance and trust level become per-chunk properties. |
| One retrieval entrance | per-channel isolation (§6) | An ACL exists for the first time; oversharing and cross-boundary injection become live (§4). |
| Per-person profiles | — (new) | A machine-written account of what a person has been working on is a different artifact from a searchable channel, especially when the reader is their supervisor. Needs a stated stance, not an accident. |

## Carry-over rules

1. Citations are resolved, never generated.
2. Provenance travels with every chunk: source, authority, and — for annotations
   — the anchor. Retrieval that mixes sources must be able to say which is which.
3. Vocabulary is scoped to its context by default; global is the exception and
   must be argued for.
4. Calibrate, don't caveat. Refusal and hedging are features; disclaimers are not.
5. Never render a model-authored URL. Links come from resolved identifiers and
   constructed permalinks only.
6. Before a boundary-crossing feature ships, name what replaces the structural
   guarantee it removes.
7. The question log decides the roadmap.

## Open questions

- **Orphaned corrections (§2).** An in-thread correction is indexed without the
  claim it corrects. Options: index bot replies but mark them non-citable
  evidence usable only as context for their thread; or attach the correction to
  the retrieved chunks the answer cited, as a negative signal on those chunks.
  The second is more interesting and harder.
- **Annotation ingest path.** Zotero syncs PDF annotations as first-class items
  in the API — verify this against the current API version before designing
  against it. If it holds, annotations arrive with anchor and author already
  attached, which is most of rule 2 for free.
- **Profile stance.** Who can retrieve a person's profile, and does the person
  see their own? Decide before the first one is generated, not after someone
  finds theirs.
- **Trust levels.** If sources of different authority coexist, is that a
  per-chunk scalar the prompt reads, or a hard retrieval partition? A scalar is
  easier and fails softly; a partition is what §6 taught us actually holds.

## Sources

- [Moffatt v. Air Canada, 2024 BCCRT 149](https://www.mccarthy.ca/en/insights/blogs/techlex/moffatt-v-air-canada-misrepresentation-ai-chatbot) · [ABA summary](https://www.americanbar.org/groups/business_law/resources/business-law-today/2024-february/bc-tribunal-confirms-companies-remain-liable-information-provided-ai-chatbot/)
- [Why Meta's latest large language model only survived three days online — MIT Technology Review](https://www.technologyreview.com/2022/11/18/1063487/meta-large-language-model-ai-only-survived-three-days-gpt-3-science/)
- [Data exfiltration from Slack AI via indirect prompt injection — PromptArmor](https://promptarmor.substack.com/p/slack-ai-data-exfiltration-from-private) · [Simon Willison's writeup](https://simonwillison.net/2024/Aug/20/data-exfiltration-from-slack-ai/)
- [Copilot didn't overshare your data, your permissions did — Petri](https://petri.com/copilot-didnt-overshare-your-data-your-permissions-did/)
- [NYC's official chatbot told businesses to break the law — The Markup / Futurism](https://futurism.com/nyc-chatbot-break-law)
- [IBM's Watson gave 'unsafe and incorrect' cancer treatment advice — STAT via Healthcare Dive](https://www.healthcaredive.com/news/stat-ibms-watson-gave-unsafe-and-incorrect-cancer-treatment-advice/528666/) · [STAT, 2017](https://www.statnews.com/2017/09/05/watson-ibm-cancer/)
- [MIT NANDA, The GenAI Divide: State of AI in Business 2025](https://finance.yahoo.com/news/mit-report-95-generative-ai-105412686.html)
