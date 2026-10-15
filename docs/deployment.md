# Deploying slackqa to the institute machine

Moving the bot off a laptop and onto an always-on machine on the campus
network. Written to be followed on the target machine, where this conversation
is not available.

> **Done once already.** Completed on **2026-10-13** onto
> `qmi-nf-cr.phas.ubc.ca` (Ubuntu 24.04, Xeon E-2124G). §12 records what
> actually went wrong, which is more useful than the happy path — read it
> before repeating this.

Read [LOG.md](../LOG.md) first for the standing decisions this plan honours —
particularly `P4`/`P5` on secrets, `P7` on the unauthenticated dashboard, and
`P2` on channel consent.

---

## 1. End state

```
   Slack  ──── outbound WSS (Socket Mode) ────┐
                                              │
                          ┌───────────────────▼────────────────────┐
                          │   institute machine (campus network)   │
                          │                                        │
   you ── ssh ───────────►│  systemd --user: slackqa.service       │
        tunnel :8765      │    ├─ Socket Mode listener             │
                          │    ├─ SQLite index   (local disk)      │
                          │    ├─ fastembed ONNX (local CPU)       │
                          │    └─ dashboard on 127.0.0.1:8765      │
                          └───┬────────────────────────────┬───────┘
                              │ https (no VPN)             │ https
                              ▼                            ▼
                   qmi-ai-assistant.phas.ubc.ca    openrouter.ai
                   GLM 5.2 — answers               fallback only
```

| Aspect | End state |
|---|---|
| **Access** | SSH on **port 7743** (not 22, which is closed), over the PHAS VPN. No jump host. |
| **Code execution** | `systemd --user` unit, restart-on-failure, starts at boot via `loginctl enable-linger`. No SSH session involved. |
| **Data storage** | SQLite at `$DATA_DIR/slackqa.db` on **local disk**, never a network home directory. Nightly backup. |
| **Ingestion** | Backfill on first run per channel; catch-up at every start; live messages, edits and deletes arrive as Socket Mode events. Glossary mining every 24 h. |
| **Listener** | One instance, on this machine only. The laptop never runs `run` again. |
| **Model calls** | GLM 5.2 on the GPU cluster over plain HTTPS — no VPN, both hosts are on campus. OpenRouter as fallback, or disabled entirely with `LOCAL_ONLY`. |
| **Embeddings** | Local CPU (fastembed ONNX). No network after the first model download. |
| **Dashboard** | `127.0.0.1:8765`, reached only through an SSH tunnel. |
| **Inbound network** | None. Socket Mode dials out; nothing listens on a public interface. |

---

## 2. Before you start: three questions for the admin

These change the plan, so ask first.

1. **Does the CPU support SSE4.2 and AVX2?** Check *before* anything else:
   `grep -o -E 'sse4_2|avx2' /proc/cpuinfo | sort -u`. The first candidate
   machine was a 2008 Xeon with only SSE4.1; numpy 2.x and `onnxruntime` both
   refuse to run, which takes `fastembed` and the whole vector half of `D3`
   with them. Nothing else about the machine matters if this fails.
2. **Is `$HOME` on NFS/AFS or local disk?** SQLite's locking is unreliable on
   network filesystems — this is a corruption risk, not a performance note. If
   home is networked, ask for a local scratch path (`/var/lib/...`,
   `/scratch/...`) and point `DATA_DIR` there.
3. **Who has root?** `.env` holds the Slack bot token, the Zotero write key and
   the OpenRouter key. `chmod 600` stops other users, not administrators. If
   that is not acceptable, ask for a dedicated service account rather than
   running under your personal login.
4. **Is `systemd --user` available, with lingering permitted?** If not, fall
   back to `tmux` (§7) and accept that a reboot needs a manual start.

---

## 3. Phase 1 — Install (no secrets yet)

```bash
ssh qmi
git clone https://github.com/Plswearpants/LAIRbot.git slackbot
cd slackbot

# A virtualenv copied from another machine is worthless and actively
# misleading — its wheels were built for that CPU. Delete it.
rm -rf .venv

curl -LsSf https://astral.sh/uv/install.sh | sh     # usually not preinstalled
export PATH="$HOME/.local/bin:$PATH"                # add this to ~/.bashrc too

uv sync                 # needs Python >= 3.11
uv run pytest -q        # expect: all tests pass, no .env required
```

Tests passing here prove the interpreter, dependencies and package layout are
sound **before** any credential exists on the machine. If something is broken,
you want to find out now rather than while debugging a dead listener.

---

## 4. Phase 2 — Transfer the irreplaceable data

Three files git deliberately does not carry. From the laptop:

```bash
cd ~/Documents/Dong/slackQA
scp .env data/slackqa.db data/glossary.md qmi:~/slackqa/transfer/
```

On the institute machine:

```bash
cd ~/slackqa
mkdir -p "$DATA_DIR"                 # or ./data
mv transfer/slackqa.db transfer/glossary.md "$DATA_DIR"/
mv transfer/.env .
chmod 600 .env
rmdir transfer
```

**Copy the database; do not rebuild it.** Re-ingesting from Slack recovers
messages, chunks and embeddings in about 20 minutes. It does **not** recover:

| Table | Rows | Why it cannot be rebuilt cheaply |
|---|---|---|
| `literature` | ~1,120 | DOI → Zotero keys and resolution state. Rebuilding means re-resolving against Crossref and arXiv at 3 s per preprint — hours, and arXiv throttles silently (`P8`). |
| `profile_audit` | small | Who endorsed or edited which profile. Exists nowhere else. |
| `query_log` | small | What the lab has actually asked. |

`data/glossary.md` carries endorsements and is likewise not regenerable.
`data/glossary.html` is only a rendered view of it — skip it and regenerate
with `uv run slackqa glossary --html` if you want it.

---

## 5. Phase 3 — Configure

Edit `.env` on the institute machine. What changes from the laptop:

```bash
DATA_DIR=/local/path/slackqa-data     # §2 q1 — local disk, not NFS

LOCAL_API_BASE=https://qmi-ai-assistant.phas.ubc.ca/api
LOCAL_MODEL=glm-5.2-nvfp4
LOCAL_ONLY=false                      # see the decision below

DASHBOARD_HOST=127.0.0.1              # leave as-is; see §7
```

**Never `source .env`.** Exported shell variables outrank the file, so a stale
export silently wins and editing the file does nothing — it also defeats the
API-key hot reload. Every command now warns when this happens; heed it. For a
one-off need, use a subshell: `( set -a; . .env; set +a; some-command )`.

### The `LOCAL_ONLY` decision

On campus the GPU cluster is directly reachable, so falling back to OpenRouter
stops being a daily event and becomes a genuine outage signal.

- `LOCAL_ONLY=false` — answers keep coming when the cluster is down, but channel
  content then leaves the lab, silently and with nothing visible in Slack.
- `LOCAL_ONLY=true` — the bot refuses rather than answering elsewhere. "Nothing
  leaves the lab" becomes a promise you can make to your colleagues.

Decide deliberately and record it in `LOG.md`. Either is defensible; drifting
into one by default is not.

---

## 6. Phase 4 — Verify before going live

Run these in order. **Do not start the listener yet** — the laptop is still
serving, and two listeners split Slack's events between them (§7).

```bash
uv run slackqa status        # index reads back: 6 channels, ~22.7k messages
uv run slackqa stats         # per-channel counts match the laptop
uv run slackqa ask CLXDY4AK1 "what were the recent issues with the tip?"
```

The first `ask` downloads the fastembed ONNX model (~90 MB) and will pause for a
minute. That is normal, once.

First confirm the model id still exists — **cluster ids change without
notice** (§12):

```bash
( set -a; . ./.env; set +a
  curl -s -H "Authorization: Bearer $LOCAL_API_KEY" "$LOCAL_API_BASE/models" \
    | python3 -m json.tool | grep '"id"' )
```

What to check in the `ask` output:

- Startup names the local cluster: `Answering with the local cluster (<model>)`
- The POST goes to `.../api/chat/completions`, **not** `/:3000/api/...`
- No `falling back` warning — if present, the GPU cluster is unreachable from
  this host and that must be fixed before cutting over
- No environment-override warning
- The answer carries permalink citations

---

## 7. Phase 5 — Cut over

**Stop the laptop listener first.** Socket Mode allows an app several open
connections, and Slack delivers each event to **one** of them. Two listeners
therefore do not reply twice — they split the traffic. Each silently misses
part of the questions and part of the messages, and their databases drift
apart with nothing in Slack to show it. Nothing prevents this; it is
discipline, not a lock.

```bash
# On the laptop
pkill -f "slackqa run"
```

Then on the institute machine:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/slackqa.service <<'EOF'
[Unit]
Description=slackqa Slack bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/slackqa
ExecStart=%h/.local/bin/uv run slackqa run
Restart=on-failure
RestartSec=30
# Slack drops the socket on network blips; restarting is the correct response.
StandardOutput=append:%h/slackqa/logs/slackqa.log
StandardError=inherit

[Install]
WantedBy=default.target
EOF

mkdir -p ~/slackqa/logs
loginctl enable-linger "$USER"      # survives logout and reboot
systemctl --user daemon-reload
systemctl --user enable --now slackqa
systemctl --user status slackqa
```

Confirm in Slack by mentioning the bot in an indexed channel. **One** reply.

### Without systemd

```bash
tmux new -s slackqa
uv run slackqa run
# detach: Ctrl-B then D
```

Survives logout, but not a reboot — you must remember to start it again.

---

## 8. Access

The dashboard stays on loopback (`P7`): it has no authentication, and the
profile pages carry endorse and edit buttons that record whatever name is typed.
On a shared machine, binding to `0.0.0.0` would expose that to every other user
on the host.

From the laptop:

```bash
ssh -N -L 8765:127.0.0.1:8765 qmi
```

Then open `http://127.0.0.1:8765` locally. `/profiles` for the profile pages,
`/health` for JSON.

---

## 9. Operations

| Task | Command |
|---|---|
| Logs | `journalctl --user -u slackqa -f` or `tail -f ~/slackqa/logs/slackqa.log` |
| Restart | `systemctl --user restart slackqa` |
| Status without a browser | `uv run slackqa status` |
| Catch up after downtime | `uv run slackqa sync` (a restart also catches up) |
| Is live ingestion working? | Post a plain message (no `@LAIRbot`) in an indexed channel; the dashboard's "last Slack event" should move within seconds. `@mentions` alone prove nothing — they arrive on a separate subscription. |
| Update the code | `git pull && uv sync && systemctl --user restart slackqa` |
| Rotate a key | Edit `.env` — the running process re-reads it, no restart needed |
| Repair the index against Slack | `uv run slackqa reconcile` (read-only, bot can stay up), review the counts, then stop the service and `uv run slackqa reconcile --apply` |
| Rebuild every chunk | Stop the service, `uv run slackqa reindex`, start it. Re-embeds ~10,800 chunks: about **30 minutes** on this CPU |

**Run anything long on the server, detached.** A sequence like "stop the
service, rebuild, start it" sent as one SSH command leaves the restart at the
mercy of your laptop: if the laptop sleeps or the VPN drops, the restart never
runs and the bot stays down. Wrap it so it finishes on the server regardless:

```bash
systemctl --user stop slackqa
nohup sh -c "uv run slackqa reindex > logs/reindex.log 2>&1; systemctl --user start slackqa" &
```

And never `pkill -f` a pattern that also appears in your own SSH command: the
whole remote script is one command line, so it matches itself and kills the
session. Bracket a character that appears only in the target's command line —
`pkill -f "bin/[s]lackqa reconcile"`.

### Backup

**This machine holds the only copy.** Nothing is kept on the laptop, so a disk
failure, a reimage, or an account that quietly expires over a holiday takes the
index, the literature ledger and every endorsement with it. Backups are the
recovery path, not a precaution.

A backup on the same disk is not a backup. Two layers:

**On the machine** — nightly, keeping a fortnight, for the ordinary case of
having broken something:

```bash
crontab -e
0 3 * * * sqlite3 $DATA_DIR/slackqa.db ".backup $HOME/slackqa/backups/db-$(date +\%F).sqlite" && find $HOME/slackqa/backups -name 'db-*' -mtime +14 -delete
```

Use `.backup`, not `cp` — copying a live SQLite file while it is being written
produces a corrupt snapshot.

**Off the machine** — **monthly, by hand, while you are at the laptop.** The
SSH key lives in Secretive's Secure Enclave and needs Touch ID for every use,
so a scheduled job would fail silently every time with nobody there to
authorise it. At this stage a monthly copy is enough, and doing it in person
avoids needing a second, unattended key:

```bash
rsync -az qmi:~/slackbot/backups/ ~/slackqa-offsite/
```

This is what survives the institute machine itself being lost. If the lab has a
managed backup service, use that as well — but confirm it actually covers your
`DATA_DIR`, which is on local disk precisely so SQLite works, and local disk is
often what such services skip.

`.env` needs somewhere you can genuinely recover it from — a password manager,
not a second copy on the same host. The Slack tokens can be regenerated; the
Zotero write key and the OpenRouter key are a nuisance to replace.

---

## 10. Rollback

**Keep the laptop copy until the institute machine has proven itself.** Do not
delete `~/Documents/Dong/slackQA` on the day you cut over. Until then it is a
complete, working fallback, and the cost of keeping it is 46 MB.

Retire it only once all three hold:

- the service has survived a reboot and restarted on its own,
- a night of backups has run and you have restored one to a scratch copy,
- the off-machine copy (§9) exists.

While it is still there, rolling back is:

```bash
systemctl --user stop slackqa      # institute machine — stop it first
# then, on the laptop
./slackqa run
```

Slack is the only shared state, so switching back costs nothing beyond whatever
was indexed on the cluster meanwhile — `slackqa sync` recovers it.

After the laptop copy is gone, rollback means restoring a backup onto some
machine, which is a slower and less certain thing. That is the reason §9 insists
on a copy that does not live on the institute machine.

---

## 11. Decisions this migration leaves open

- **`LOCAL_ONLY`** (§5) — whether "nothing leaves the lab" is a promise or a
  tendency.
- **Service account vs personal login** (§2 q2) — who can read the tokens.
- **Dashboard reachability** — loopback plus SSH tunnel works for you and not
  for the lab. Making it available to colleagues needs the Slack-signed-links
  work in `LOG.md` first; do not expose the port as a shortcut.
- **`profiles/` in git** — now tracked, so colleagues' profiles are in history
  permanently. Fine for a private repo; worth a deliberate note either way.

---

## 12. What actually went wrong (2026-10-13)

Four things, none of which the plan anticipated. Three were silent.

### The first machine was unusable, and we found out late

`qmi-463` was a **2008 Xeon E5462** — SSE4.1, no SSE4.2, no AVX. numpy 2.x ships
wheels built for `x86-64-v2` and refuses to import; `conftest.py` imports numpy
at line 3, so the test suite could not run *at all*. `onnxruntime` needs SSE4.2
too, so `fastembed` was equally dead.

Nothing could be configured around it — `lscpu` showed no hypervisor, so it was
real silicon with no CPU model to change. The replacement, `qmi-nf-cr`, is a
2018 Xeon E-2124G with `sse4_2 avx avx2 f16c fma`, and everything worked
immediately.

**Lesson:** the two-line CPU check is now prerequisite #1. It costs five
seconds and it decides whether the machine is viable.

### There was no gateway; sshd was on a non-standard port

The admin said access was through the UBC SSH Proxy gateway. In fact **sshd
listens on 7743** and port 22 is closed, exactly as on `qmi-463`. Over the PHAS
VPN it is directly reachable and no jump host is involved.

Time was lost trying `ssh.phas.ubc.ca` as a `ProxyJump`: it accepts a CWL
password but has `AllowTcpForwarding no`, so it cannot forward to a third host
(`administratively prohibited: open failed`). It is a login host, not a proxy.

The central UBC SSH Proxy hostname is still unknown and only matters off VPN.
Do not work around falcon's forwarding block — that restriction is deliberate.

### The cluster renamed its models

`.env` asked for `Qwen3.8-27B-FP8`. The cluster had been upgraded and now
serves `GLM-5.3-NVFP4` and `DeepSeek-V4.1-Flash`. The old id returned
**HTTP 400 "Model not found"**, the fallback engaged, and every answer came
from OpenRouter — correctly formatted, properly cited, and from the wrong
provider.

**Lesson:** re-check `$LOCAL_API_BASE/models` after any cluster change, and
treat the dashboard's `N/M answered off-site` counter as the signal that
something is wrong. Nothing in Slack looks different when this happens.

### GLM-5.3 is a reasoning model, and the token budget was too small

With a valid id the cluster returned HTTP 200 and an **empty completion**.
GLM-5.3 emits `reasoning_content` beside `content`, and reasoning tokens are
billed against the same `max_tokens`. A one-sentence question spent 271 tokens
thinking; a RAG-sized prompt consumed the whole 2,048 budget on reasoning and
left nothing for the answer.

`MAX_ANSWER_TOKENS=6144` fixes it. Any future reasoning model needs the same
headroom — budget for thinking *plus* answer, not just the answer.

### The common thread

Three of the four were invisible from Slack. Answers kept arriving, cited and
plausible, while the local backend was entirely bypassed. A working bot is not
evidence of a working configuration — check the backend the dashboard reports,
not whether replies appear.

### Plain messages stopped arriving (found 2026-10-15)

Two days after cutover, five channels held messages the index did not. The
socket was healthy, Slack's five-hourly session rotations were clean, and
`@LAIRbot` questions were answered normally — but no plain channel message had
reached the handler since the restart. Six genuine misses, including Jisun's
tip crash in `#tesla`; the other eight unindexed messages were bot posts and
questions to LAIRbot, excluded by design (`P1`, `is_indexable`).

The two kinds of event come from separate subscriptions: `app_mention` and
`message.channels`. A bot that answers questions proves only the first.
`slackqa sync` recovered everything. **Cause confirmed:** `message.channels`
had never been subscribed — only `app_mention`. Added at api.slack.com → Event
Subscriptions → Subscribe to bot events; no reinstall was needed, since it uses
the `channels:history` scope the bot already had. A plain test message was
indexed and embedded within seconds of being posted.

What the missing subscription cost, for the whole life of the project: edits
were never picked up, and deletions were caught only by the 30-day reconcile
at startup — so messages deleted when older than 30 days, and every edited
message's original wording, may still be in the index. A full reconcile
against Slack is needed to repair that.

**Lesson:** an always-on service is the first deployment where nothing
restarts it, so catch-up-at-startup stops quietly covering for missing live
events. Verify ingestion with a plain message, not a question.

### The reconcile exposed a chunking bug (2026-10-15)

Applying the reconcile added 70 thread replies correctly, and left 14 other
messages outside every chunk — stored, but unsearchable. `reindex_window`
pulled a whole thread in, widened its time range to the thread's span, and
deleted every chunk in that range while having loaded only the window's
messages and the thread's. Everything else said in between lost its chunk.

The live path had the same bug, active from the moment `message.channels` was
subscribed: one reply today to a 2023 thread would have done this to most of a
channel. Threads are now rebuilt whole by identity and windows by time, and
the tests assert that incremental rebuilds equal a full rebuild. `slackqa
reindex` repaired production.

The verification that caught it — comparing orphaned and duplicated chunks
against the pre-apply backup — is worth repeating after any bulk change. A
count that comes out right is not evidence the chunks are right.
