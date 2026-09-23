---
description: One sweep for new/updated PR events — review and post. Wrap in /loop to watch continuously.
---

Run a single PR-event sweep with the `pr-reviewer` agent.

Scope: every open pull request in the repos listed in `.claude/targets.txt`
(the agent's default scope), unless the invocation names a specific repo or
PR, in which case review only that. Do not widen to every repo the account
owns — that turns a bounded sweep into a 60-repo enumeration on every tick.

A "new PR event" means either:

- a PR you have not reviewed at all, or
- a PR whose current head SHA differs from the SHA of your last review on it.

Skip everything else — that idempotency check is what makes it safe to run
this repeatedly on a short interval without spamming duplicate reviews. Use
the agent's single-call enumeration loop (`gh` CLI) for it, before fetching
any diff.

For each PR that qualifies, follow the `pr-reviewer` agent's procedure: read
the actual diff, review for the security and quality issues it enumerates,
and post a real GitHub review — not a local write-up. PRs opened by the same
account are posted as `COMMENT` with the verdict in the body.

Finish with a short report: repos swept, PRs reviewed vs skipped (and why),
and a one-line verdict per PR reviewed with a link. If nothing changed since
the previous sweep, say exactly that in one line and stop — do not pad the
report.
