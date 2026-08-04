# agents

Claude Code subagents and supporting tooling.

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
```

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
```

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
