# agents

Claude Code subagents and supporting tooling.

## pr-reviewer

Reviews open GitHub pull requests — by default every repo owned by
`robertncl` — for security and code quality issues, then posts the findings
as a real review on the PR (not just a local report).

It's invoked on demand, not a persistent webhook listener: each run sweeps
open PRs, skips ones it already reviewed at the current head commit, and
posts a fresh review (`REQUEST_CHANGES`/`COMMENT`/`APPROVE`) on the rest. For
continuous coverage, re-run it on a schedule via the `schedule`/cron skill.

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
```

## dependabot-fixer

Processes a repository's open Dependabot security alerts and lands **one
branch, commit, and pull request per vulnerability**, so each fix can be
reviewed, merged, or reverted on its own.

Alerts come from `gh api /repos/{owner}/{repo}/dependabot/alerts` (they aren't
exposed via the GitHub MCP tools). Alerts on the same package are grouped into
a single PR; work is ordered critical → high → medium → low.

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
```

## dependency-updater

Reviews a git repository and updates dependencies and GitHub Actions to the
latest version that has **survived a cooloff window** — 24 hours by default.

The threat it addresses: most registry supply chain attacks (compromised
maintainer account, hijacked action tag, typosquat) are detected and yanked
within hours of publication. Refusing to adopt anything younger than a day
removes the window the attacker is counting on, at the cost of being one day
behind latest.

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
```

### scripts/cooloff.py

The version resolver the agent relies on. Usable standalone and in CI:

```bash
# newest version at least 24h old
scripts/cooloff.py pkg -e npm -n react -c 18.2.0 --json
scripts/cooloff.py pkg -e pypi -n requests

# newest action release at least 24h old, resolved to a commit SHA
scripts/cooloff.py action actions/checkout@v4 --json

# audit pin state of every `uses:` in .github/workflows (exit 2 if unpinned)
scripts/cooloff.py scan-actions --dir .
```

Ecosystems: `npm`, `pypi`, `crates`, `rubygems`, `go`, `maven`, `nuget`.

Flags: `--hours N` (window, or `$COOLOFF_HOURS`), `--same-major` (no major
bumps), `--allow-prerelease` (off by default), `--json`.

Exits non-zero when nothing clears the cooloff, so `set -e` scripts fail closed
rather than silently installing something unvetted. Yanked, deprecated, and
unlisted versions are skipped. Action dates use the latest of commit date,
annotated-tag date, and release date, so a tag pointing at an old commit cannot
understate its age.

Requires Python 3.10+ and, for the `action` subcommand, an authenticated `gh`.
