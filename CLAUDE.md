# slackQA

A Slack bot that answers questions from a channel's own history, with permalink
citations, and refuses when the channel doesn't support an answer. See
[README.md](README.md) for setup and [SPEC.md](SPEC.md) for design decisions and
their rationale.

## Product log

[LOG.md](LOG.md) is a living record: what was built and why, what is next, and
the standing decisions — build philosophy in `D*`, privacy and security in `P*`.

**Read it before proposing a direction**, so settled questions are not
re-litigated, and **add an entry at the top of its session log at the end of
each working session**. Cite decisions by ID rather than restating them; when
one is overturned, strike it through and keep it — that we once thought
otherwise is part of the record.

## Deployment

[docs/deployment.md](docs/deployment.md) is the runbook for the institute
machine: end state, transfer steps, the systemd unit, and the single-listener
rule. Keep it current when the deployment changes.

## Agent skills

### Issue tracker

Issues and PRDs live as GitHub issues, driven through the `gh` CLI. See
`docs/agents/issue-tracker.md`.

> `origin` is the **private** repo `Plswearpants/LAIRbot`. It must stay private:
> `SPEC.md`, `README.md` and `docs/deployment.md` name the workspace URL,
> channel IDs, the GPU cluster host and lab specifics.

### Triage labels

The five canonical roles, each label string equal to its name. See
`docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See
`docs/agents/domain.md`.
