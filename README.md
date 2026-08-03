# agents

Claude Code subagents and supporting tooling.

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
