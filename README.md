# agents

Claude Code subagents and supporting tooling.

| Agent | Does | Writes to your repo? |
| --- | --- | --- |
| [`dependabot-fixer`](#dependabot-fixer) | Turns open Dependabot alerts into minimal fix PRs | PR per direct fix + one transitive PR per manifest (never merges) |
| [`dependency-updater`](#dependency-updater) | Routine dependency/Action upgrades past a 24h cooloff | Branch + PR when there's anything to update (never merges) |
| [`pr-reviewer`](#pr-reviewer) | Reviews open PRs for security + quality, posts a real GitHub review | Review comments only |

All three agents are pinned to `model: claude-sonnet-5-5` (Sonnet 5.5). The
dependency agents previously ran on Haiku, but the 2026-10-01 sweep showed the
gaps in procedure-following alone: one edited files in an untracked nested
checkout, several bundled major bumps (`@types/jasmine` 6→7, Expo 57→58 — a
`next`-tagged SDK) into routine PRs, and one reported a repo current while
ten lookups had timed out. They now share `pr-reviewer`'s model, whose steps 4
and 5 need genuine security judgment rather than procedure.

## Sweep scope

With no repo named, all three sweep exactly the repos listed in
[`.claude/targets.txt`](.claude/targets.txt) — never every repo the account
owns. A repo earns a line there only if it is **not a fork**, not archived,
write-accessible, and a **previous sweep actually found something to fix**
(the evidence PR is in the trailing comment).

Naming a repo explicitly (`path`, `owner/repo`, or clone URL) bypasses the list
and targets just that repo. The fork/archive/permission guard still runs either
way — a fork is always skipped, listed or not.

To add a repo, verify all four conditions and append it with its evidence PR.
Adding speculative targets costs a full clone-and-resolve pass for nothing.

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

Reviews open GitHub pull requests — by default the repos in
[`.claude/targets.txt`](.claude/targets.txt) — for security and code quality
issues, then posts the findings as a real review on the PR (not just a local
report). Sweeping every repo the account owns is opt-in, by asking for it.

It's invoked on demand, not a persistent webhook listener: each run sweeps
open PRs, skips ones it already reviewed at the current head commit, and
posts a fresh review (`REQUEST_CHANGES`/`COMMENT`/`APPROVE`) on the rest. PRs
opened by the same account — including every PR the other two agents open — are
posted as `COMMENT` with the verdict in the body, since GitHub rejects approving
or requesting changes on your own PR. It drives GitHub through the `gh` CLI.

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

Processes a repository's open Dependabot security alerts and lands the
**smallest safe upgrade that closes each one**: one pull request per
direct-dependency fix, so each can be reviewed, merged, or reverted on its own,
plus one combined pull request per manifest for transitive fixes that only move
the lockfile (separate lockfile-only PRs would conflict after the first merge).

Alerts come from `gh api "/repos/{owner}/{repo}/dependabot/alerts?state=open"`
(they aren't exposed via the GitHub MCP tools). The scope gate and the open-alert
count for every target repo come back from one call, so repos with nothing open
cost one line rather than a full pass. Alerts on the same package are grouped into
a single PR; work is ordered critical → high → medium → low.

The alert list is fed straight into `cooloff.py batch`, so every fix version —
packages and vulnerable Actions alike — resolves in one concurrent pass before
any branch is created. That means the full shape of the run (how many PRs, what
is held back, what has no fix) is known up front rather than discovered one
package at a time.

What it does:

- Treats `first_patched_version` as a floor and resolves the newest release
  on the floor's **major line** (`batch --same-major` with the floor as
  `@current`) that clears the same 24h cooloff window as `dependency-updater`.
  It crosses a major only when no backport exists, and then only to the
  floor's major. A CVE patch published minutes ago gets held back and surfaced
  as a tradeoff, not adopted silently.
- Never treats "newer than the vulnerable range" as a fix when the advisory
  names no patched version. Such advisories list every release up to the
  newest, so the next release is outside the range whether or not it fixes
  anything (astro#20/#21). It opens a PR there only on a cited upstream fix;
  otherwise the alert is `no fix available`, reported with the parent chain
  that pulls the package in.
- Fixes transitive alerts in place: a targeted lockfile update when the
  parent's range allows it, else a same-major parent bump, else an
  `overrides`/`resolutions` stopgap. It never adds a transitive package as a
  direct dependency, and proves with `npm ls`/`pnpm why` that no vulnerable
  copy remains.
- Never uses `--legacy-peer-deps`/`--force` to get an install through, reports
  the exact build/test commands it ran, and processes every open alert rather
  than stopping at a self-chosen batch.
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

Forks are refused, not just discouraged — the agent checks `isFork` before
touching anything and skips, because dependency drift on a fork should track
upstream rather than being pushed independently.

### scripts/cooloff.py

The version resolver the agent relies on. Usable standalone and in CI:

```bash
# whole repo in one pass: every manifest + every workflow `uses:`. Saves every
# row to batch.json and prints only the rows that need attention.
scripts/cooloff.py scan-deps --dir . | scripts/cooloff.py batch - --out batch.json

# just the inventory, one `ecosystem:name@current` spec per line
scripts/cooloff.py scan-deps --dir .

# single lookups
scripts/cooloff.py pkg -e npm -n react -c 18.2.0 --json
scripts/cooloff.py pkg -e pypi -n requests
scripts/cooloff.py action actions/checkout@v4 --json

# is each exact version (say, every entry a lockfile regen added) past the window?
scripts/cooloff.py check-age transitive.txt        # exit 5 if any is too new

# audit pin state of every `uses:` in .github/workflows (exit 2 if unpinned)
scripts/cooloff.py scan-actions --dir .

# apply saved batch results to every workflow `uses:` in one pass
scripts/cooloff.py pin-actions batch.json --dir . --skip-majors [--dry-run]
```

`pin-actions` never resolves anything itself: it rewrites each external
`uses:` to `owner/repo[/path]@<sha> # <tag>` from rows `batch` already
resolved, lists any unpinned ref it had no row for (held back or errored), and
exits 4 without writing if an existing pin's SHA no longer matches its tag
comment — a moved tag is a compromise signal, not an update. `--skip-majors`
leaves rewrites that would cross a major version alone and lists them, so a
routine sweep can always run it.

`check-age` answers a different question from `batch`. `batch` asks "what is
the newest version that has cleared the window?". `check-age` asks "has *this*
version cleared it?". That second question is the one a regenerated lockfile
poses. Run through `batch`, a locked version still inside the window comes
back `ahead` or `update`, never `held_back`. `check-age` labels each spec
`ok`, `too_new` (with `hours_short`), or `unknown` (not a listed release:
yanked, deprecated or unpublished). It exits 5 if any is too new and 3 if any
is unknown.

`scan-deps` reads `package.json`, `requirements*.txt`, `pyproject.toml`
(PEP 621 and Poetry), `Cargo.toml`, `go.mod`, `Gemfile`, `*.csproj`/`*.fsproj`,
and `pom.xml`, skipping vendored trees, and deduplicates packages that appear
in more than one manifest. Anything it can't parse — a Maven `${property}`
version, or a TOML manifest on Python 3.10 without `tomllib` — is reported as a
`note:` on stderr rather than silently dropped.

`batch` resolves specs concurrently (`-j`, default 8) behind a shared response
cache. Specs come as arguments, `-` for stdin, or a path to a file of specs. It
labels each row `update`, `current`, `resolved` (nothing pinned to compare
against), `held_back` (nothing newer has cleared the window at all),
`peer_held`, `above_ceiling`, `ahead`, or `error`. Every row, actions and
errors included, carries `kind`, `ecosystem`, `name`, `current` and `target`.
A newer release still inside the window usually rides on an `update` or
`current` row as `skipped_too_new`, each entry with `hours_short`. The text
table shows these as `; held back: <version> (<N>h short)`. `--out FILE`
writes the JSON to FILE and prints only the rows that need attention. Retries
cover dropped or truncated registry responses and transient `gh` failures,
not 4xx answers. It exits 3 if any row errored — the other rows still
resolved. Empty input (`scan-deps` on a repo with no manifests) prints
`nothing to resolve` (`[]` with `--json`) and exits 0.

Maven versions come from the artifact's `maven-metadata.xml`, which is what
Maven itself resolves against. search.maven.org only supplies dates. Its index
lags by months: in October 2026 it topped out at spotless-maven-plugin 2.44.5
while 3.10.4 was live. Versions it is missing are dated from their POM's
`Last-Modified`.

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

### scripts/verify_alerts.py

The dependabot-fixer's post-push check. It reads the lockfile for each alert's
manifest **from GitHub at a given ref**, not from a local checkout, so neither
an unpushed working tree nor a sibling agent's clone can make a fix look done.
npm alerts are read from `package-lock.json`/`pnpm-lock.yaml`. pip alerts are
read from the pinned `requirements*.txt` itself, or `poetry.lock`, `uv.lock`
or `Pipfile.lock` beside the manifest. A lockfile is only ever read for its
own ecosystem's alerts:

```bash
# every open alert against a PR branch
scripts/verify_alerts.py robertncl/JS dependabot-fix/npm-express-and-node-fetch

# only the alerts a PR claims; exit 1 if any is still vulnerable
scripts/verify_alerts.py robertncl/node1 dependabot-fix/npm-transitive-root --alerts 1,2,5
```

Each alert is `fixed` (no installed copy, nested ones included, is inside the
advisory's `vulnerable_version_range`), `vulnerable` (lists the offending
versions), `unconfirmed` (outside the range, but the advisory names no patched
version, so the newer release is not evidence of a fix), `inconsistent` (an
npm entry's `version` disagrees with the tarball its `resolved` URL points at
-- a hand-edited lockfile that still installs the old code), or `unsupported`
(no lockfile it reads for that ecosystem; check by hand). It compares against
the range rather than `first_patched_version`, so a fix backported to an older
line (picomatch 2.3.2 against a 4.0.4 floor) counts as fixed. Reads npm
lockfile v1–v3, pnpm v5, v6, and v9, pip requirements pins, poetry.lock,
uv.lock, and Pipfile.lock. Needs an authenticated `gh`.
