# agents

Claude Code subagents and supporting tooling.

| Agent | Does | Writes to your repo? |
| --- | --- | --- |
| [`dependabot-fixer`](#dependabot-fixer) | Turns open Dependabot alerts into one fix PR per vulnerability | Branch + PR per alert (never merges) |
| [`dependency-updater`](#dependency-updater) | Routine dependency/Action upgrades past a 24h cooloff | Branch + PR when there's anything to update (never merges) |
| [`pr-reviewer`](#pr-reviewer) | Reviews open PRs for security + quality, posts a real GitHub review | Review comments only |

## Usage

The agents live in `.claude/agents/`, so they're available to Claude Code
whenever you're working in this repo. To use them anywhere, copy them to your
personal agent directory:

```bash
cp .claude/agents/*.md ~/.claude/agents/
```

Claude Code loads agents at session start — restart (or start a new session)
after adding one. Then just describe the task and Claude picks the agent, or
name it explicitly:

```
> use the dependabot-fixer agent on robertncl/my-service
> use the dependency-updater agent on ~/code/my-service
> use the pr-reviewer agent to sweep robertncl's open PRs
```

They compose into one security-maintenance pass over a repo:

```
> use the dependabot-fixer agent on ~/code/my-service to clear the open alerts
> now use the dependency-updater agent on ~/code/my-service for everything else
> then use the pr-reviewer agent on robertncl/my-service to review what landed
```

That order matters: fix the known CVEs first as isolated PRs, do the routine
sweep second so it doesn't bundle security fixes into an unrelated diff, and
review last. All three share the same cooloff rule, so nothing published in the
last 24 hours enters the repo along the way.

None of them is a background watcher — each run is a fresh sweep, so recurring
coverage means re-running them. `/pr-watch` wrapped in `/loop` covers the review
side ([below](#watching-for-new-pr-events)); for the other two, use the
`schedule`/cron skill:

```
> /schedule every weekday at 9am: use the dependabot-fixer agent on robertncl/my-service
```

## pr-reviewer

Reviews open GitHub pull requests — by default every repo owned by
`robertncl` — for security and code quality issues, then posts the findings
as a real review on the PR (not just a local report).

It's invoked on demand, not a persistent webhook listener: each run sweeps
open PRs, skips ones it already reviewed at the current head commit, and
posts a fresh review (`REQUEST_CHANGES`/`COMMENT`/`APPROVE`) on the rest.

What it checks:

- Security: injection, hardcoded secrets, broken auth/authorization, unsafe
  deserialization, SSRF/path traversal, weak crypto, risky dependency bumps,
  leaky logging, loosened CORS/TLS/CSRF.
- Quality: missing tests, unhandled error paths, dead code, logic bugs,
  resource leaks, and consistency with the repo's existing conventions.

Invoke it by asking Claude Code to review PRs, or explicitly:

```
> use the pr-reviewer agent to sweep robertncl's open PRs
> use the pr-reviewer agent on robertncl/some-repo#42
> use the pr-reviewer agent on this repo's open PRs, security findings only
> review PR 42 with the pr-reviewer agent and focus on the auth changes
```

Naming a specific repo or PR keeps it scoped there — it won't silently expand
to every repo you own.

### Watching for new PR events

`/pr-watch` runs one sweep: it reviews PRs that are new or have new commits
since its last review, and skips the rest. Wrap it in `/loop` to keep watching
until you stop it:

```
> /loop 15m /pr-watch      # sweep every 15 minutes
> /loop /pr-watch          # let Claude pace the interval itself
```

The watch is **manually started and runs until you stop the loop** — nothing
fires it in the background on your behalf. Because each sweep skips any PR
already reviewed at its current head SHA, a short interval costs a cheap
no-op check rather than duplicate reviews.

## dependabot-fixer

Processes a repository's open Dependabot security alerts and lands **one
branch, commit, and pull request per vulnerability**, so each fix can be
reviewed, merged, or reverted on its own.

Alerts come from `gh api /repos/{owner}/{repo}/dependabot/alerts` (they aren't
exposed via the GitHub MCP tools). Alerts on the same package are grouped into
a single PR; work is ordered critical → high → medium → low.

The alert list is fed straight into `cooloff.py batch`, so every fix version —
packages and vulnerable Actions alike — resolves in one concurrent pass before
any branch is created. That means the full shape of the run (how many PRs, what
is held back, what has no fix) is known up front rather than discovered one
package at a time.

What it does:

- Treats `first_patched_version` as a floor, then resolves the newest version
  above it that clears the same 24h cooloff window as `dependency-updater` —
  a CVE patch published minutes ago gets held back and surfaced as a tradeoff,
  not adopted silently.
- Prefers fixing transitive alerts by bumping the direct parent dependency;
  `overrides`/`resolutions` are a last-resort stopgap and get labelled as one.
- Re-checks the regenerated lockfile's newly added transitive entries against
  the cooloff, and pins vulnerable GitHub Actions to full commit SHAs.
- Skips packages Dependabot already has an open PR for, and branches every fix
  off a fresh default branch so the PRs stay independently mergeable.
- Never merges, auto-merges, or dismisses an alert; opens a draft PR (flagged)
  when the test suite fails after a bump.

Skipped by design: forks, archived repos, and repos where the token lacks write
access or `security_events` scope.

```
> use the dependabot-fixer agent on robertncl/my-service
> use the dependabot-fixer agent on this repo, critical and high alerts only
> use the dependabot-fixer agent on ~/code/my-service but just report — no PRs yet
> the lodash patch is 6h old and actively exploited; rerun dependabot-fixer with --hours 1
```

That last one is the only way a sub-cooloff version gets adopted: the agent
holds it and hands you the tradeoff, and you decide.

## dependency-updater

Reviews a git repository and updates dependencies and GitHub Actions to the
latest version that has **survived a cooloff window** — 24 hours by default.

The threat it addresses: most registry supply chain attacks (compromised
maintainer account, hijacked action tag, typosquat) are detected and yanked
within hours of publication. Refusing to adopt anything younger than a day
removes the window the attacker is counting on, at the cost of being one day
behind latest.

It discovers every manifest in the tree in one pass, resolves all dependencies
concurrently, and **opens a PR when there is anything to update** (it never
merges — a human approves supply-chain changes).

What it does:

- Pins every GitHub Action to a full 40-char commit SHA with the tag in a
  trailing comment (`uses: actions/checkout@8f4b... # v5.0.1`), since tags are
  mutable and SHAs are not.
- Resolves package versions to exact pins backed by a committed lockfile, and
  re-checks newly introduced transitive dependencies against the same cooloff.
- Reports what it held back and by how many hours, so you know what to revisit.

Invoke it by asking Claude Code to update dependencies in a repo, or explicitly:

```
> use the dependency-updater agent on ~/code/my-service
> use the dependency-updater agent on https://github.com/robertncl/my-service
> use the dependency-updater agent on this repo, patches and minors only
> use the dependency-updater agent to pin the GitHub Actions in .github/workflows
```

Don't point it at a fork of someone else's repo — dependency drift on a fork
should track upstream rather than being pushed independently.

### scripts/cooloff.py

The version resolver the agent relies on. Usable standalone and in CI:

```bash
# whole repo in one pass: every manifest + every workflow `uses:`
scripts/cooloff.py scan-deps --dir . | scripts/cooloff.py batch - --json

# just the inventory, one `ecosystem:name@current` spec per line
scripts/cooloff.py scan-deps --dir .

# single lookups
scripts/cooloff.py pkg -e npm -n react -c 18.2.0 --json
scripts/cooloff.py pkg -e pypi -n requests
scripts/cooloff.py action actions/checkout@v4 --json

# audit pin state of every `uses:` in .github/workflows (exit 2 if unpinned)
scripts/cooloff.py scan-actions --dir .
```

`scan-deps` reads `package.json`, `requirements*.txt`, `pyproject.toml`
(PEP 621 and Poetry), `Cargo.toml`, `go.mod`, `Gemfile`, `*.csproj`/`*.fsproj`,
and `pom.xml`, skipping vendored trees, and deduplicates packages that appear
in more than one manifest. Anything it can't parse — a Maven `${property}`
version, or a TOML manifest on Python 3.10 without `tomllib` — is reported as a
`note:` on stderr rather than silently dropped.

`batch` resolves specs concurrently (`-j`, default 8) behind a shared response
cache, and labels each row `update`, `current`, `resolved` (nothing pinned to
compare against), `held_back` (newer version still inside the window), or
`error`. It exits 3 if any row errored — the other rows still resolved.

Ecosystems: `npm`, `pypi`, `crates`, `rubygems`, `go`, `maven`, `nuget`, plus
`action:` specs in batch input.

Flags: `--hours N` (window, or `$COOLOFF_HOURS`), `--same-major` (no major
bumps), `--allow-prerelease` (off by default), `--json`.

Exits non-zero when nothing clears the cooloff, so `set -e` scripts fail closed
rather than silently installing something unvetted. Yanked, deprecated, and
unlisted versions are skipped. Action dates use the latest of commit date,
annotated-tag date, and release date, so a tag pointing at an old commit cannot
understate its age.

Requires Python 3.10+ and, for the `action` subcommand, an authenticated `gh`.
