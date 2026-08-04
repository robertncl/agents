---
description: One sweep for new/updated PR events — review and post. Wrap in /loop to watch continuously.
---

Run a single PR-event sweep with the `pr-reviewer` agent.

Scope: every open pull request across repositories owned by `robertncl`
(confirm the account with `get_me` — don't hardcode it; skip forks and
archived repos), unless the invocation names a specific repo or PR, in which
case review only that.

A "new PR event" means either:

- a PR you have not reviewed at all, or
- a PR whose current `head.sha` differs from the SHA of your last review on it.

Skip everything else — that idempotency check is what makes it safe to run
this repeatedly on a short interval without spamming duplicate reviews.

For each PR that qualifies, follow the `pr-reviewer` agent's procedure: read
the actual diff, review for the security and quality issues it enumerates,
and post a real GitHub review (`REQUEST_CHANGES` / `COMMENT` / `APPROVE`) —
not a local write-up.

Finish with a short report: repos swept, PRs reviewed vs skipped (and why),
and a one-line verdict per PR reviewed with a link. If nothing changed since
the previous sweep, say exactly that in one line and stop — do not pad the
report.
