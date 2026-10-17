#!/usr/bin/env python3
"""Check a pushed branch's lockfiles against a repo's open Dependabot alerts.

The point: a fix is what the *pushed* lockfile resolves, not what a local
working tree showed. Past runs reported "30/30 fixed" for a PR whose committed
lockfile still held the vulnerable versions, and read a sibling agent's
checkout instead of their own. This reads the branch straight from GitHub, so
neither mistake can make it pass.

    verify_alerts.py <owner/repo> <ref> [--alerts 3,7,12] [--json]

For every open alert (or only those listed in --alerts) it reports:
  fixed        no installed copy of the package is inside the vulnerable range
  vulnerable   at least one installed copy still is (versions listed)
  unconfirmed  nothing installed is inside the range, but the advisory names
               no patched version -- every release up to the newest was
               vulnerable when it was published, so a newer one falling
               outside `<= X` is not evidence of a fix
  unsupported  no lockfile this script reads for the alert's ecosystem;
               verify by hand
  inconsistent a lockfile entry's `version` disagrees with its `resolved`
               tarball -- hand-edited; npm still installs the old code

Reads npm (package-lock.json, pnpm-lock.yaml) and pip (a pinned
requirements*.txt manifest itself, else poetry.lock / uv.lock / Pipfile.lock
beside it). A lockfile is only ever read for its own ecosystem's alerts.

Exit 0 when every checked alert is fixed, 1 when any is vulnerable,
unconfirmed, inconsistent, or unsupported, 2 on usage or API errors.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None


def open_alerts(repo: str) -> list[dict]:
    # Cursor-paginated endpoint: `page=` is rejected, --paginate follows links.
    path = f"repos/{repo}/dependabot/alerts?state=open&per_page=100"
    out = subprocess.run(["gh", "api", "--paginate", path, "--jq", ".[]"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh api {path}: {out.stderr.strip()}")
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def gh_raw(repo: str, path: str, ref: str) -> str | None:
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{path}?ref={ref}",
         "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, text=True,
    )
    return out.stdout if out.returncode == 0 else None


# --- versions and advisory ranges -------------------------------------------

def parse_version(v: str) -> tuple:
    """Semver-ish key. A prerelease sorts before its release.

    Also reads PEP 440's unseparated suffixes (`2.0.0rc1`), which the split
    on `-` alone took for the final release.
    """
    v = v.strip().lstrip("v").split("+", 1)[0]
    m = re.match(r"(\d+(?:\.\d+)*)(.*)$", v)
    core, pre = (m.group(1), m.group(2).lstrip("-._")) if m else (v, "")
    if re.match(r"(post|rev|r)\d*$", pre, re.I):  # 1.0.post1 is after 1.0
        pre = ""
    nums = [int(x) if x.isdigit() else 0 for x in core.split(".")]
    nums = (nums + [0, 0, 0])[:3]
    return (*nums, 0 if pre else 1, pre)


def in_range(version: str, advisory_range: str) -> bool:
    """GitHub advisory range, e.g. '>= 2.0.0, < 2.1.4' or '<= 1.1.11'."""
    v = parse_version(version)
    for cond in advisory_range.split(","):
        m = re.match(r"\s*(>=|<=|>|<|=)?\s*(\S+)\s*$", cond)
        if not m:
            raise ValueError(f"unparseable range: {advisory_range!r}")
        op, bound = m.group(1) or "=", parse_version(m.group(2))
        ok = {">=": v >= bound, "<=": v <= bound, ">": v > bound,
              "<": v < bound, "=": v == bound}[op]
        if not ok:
            return False
    return True


# --- lockfile readers: name -> set of installed versions ---------------------

def npm_versions(text: str) -> dict[str, set[str]]:
    lock = json.loads(text)
    found: dict[str, set[str]] = {}

    def add(name, ver):
        if name and ver and not str(ver).startswith(("npm:", "file:", "link:", "git")):
            found.setdefault(name, set()).add(str(ver))

    # lockfileVersion 2/3: flat "packages" keyed by node_modules path.
    for key, meta in (lock.get("packages") or {}).items():
        if "node_modules/" in key:
            add(meta.get("name") or key.rsplit("node_modules/", 1)[1], meta.get("version"))

    # lockfileVersion 1: nested "dependencies".
    def walk(deps):
        for name, meta in (deps or {}).items():
            add(name, meta.get("version"))
            walk(meta.get("dependencies"))
    if not lock.get("packages"):
        walk(lock.get("dependencies"))
    return found


TARBALL = re.compile(r"/-/[^/]+?-(\d+\.\d+\.\d+[^/]*?)\.tgz$")


def npm_mismatches(text: str) -> dict[str, list[str]]:
    """Entries whose `resolved` tarball is a different version than `version`.

    A hand-edited lockfile bumps `version` and leaves `resolved`/`integrity`
    on the old tarball, so npm keeps installing the vulnerable code. Reading
    `version` alone calls that fixed (node1 #3-#6 did exactly this).
    """
    lock = json.loads(text)
    bad: dict[str, list[str]] = {}

    def check(name, meta):
        m = TARBALL.search(str(meta.get("resolved") or ""))
        if name and m and meta.get("version") and m.group(1) != meta["version"]:
            bad.setdefault(name, []).append(f"{meta['version']} (tarball {m.group(1)})")

    for key, meta in (lock.get("packages") or {}).items():
        if "node_modules/" in key:
            check(meta.get("name") or key.rsplit("node_modules/", 1)[1], meta)

    def walk(deps):
        for name, meta in (deps or {}).items():
            check(name, meta)
            walk(meta.get("dependencies"))
    if not lock.get("packages"):
        walk(lock.get("dependencies"))
    return bad


PNPM_KEY = re.compile(r"^  '?/?([^\s:']+?)'?:\s*$")


def pnpm_versions(text: str) -> dict[str, set[str]]:
    """Keys under packages:/snapshots:, across lockfile v5 (/name/1.0.0),
    v6 (/name@1.0.0(peer)) and v9 ('name@1.0.0')."""
    found: dict[str, set[str]] = {}
    section = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            section = line.rstrip(":").strip()
            continue
        if section not in ("packages", "snapshots"):
            continue
        m = PNPM_KEY.match(line)
        if not m:
            continue
        key = re.sub(r"\(.*$", "", m.group(1))
        # v5 first: /name/1.0.0_peer@2.0.0 — the peer suffix has its own '@'.
        v5 = re.match(r"^(@?[^@/]+(?:/[^@/]+)?)/(\d[^_/]*)(?:_.*)?$", key)
        if v5:
            name, ver = v5.groups()
        else:
            name, _, ver = key.rpartition("@")
        if name and ver[:1].isdigit():
            found.setdefault(name, set()).add(ver)
    return found


def pip_name(name: str) -> str:
    """PEP 503 normalisation: `PyYAML`, `pyyaml` and `py_yaml` are one project."""
    return re.sub(r"[-_.]+", "-", name).lower()


REQ_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*===?\s*([^\s;\\]+)")


def requirements_versions(text: str) -> dict[str, set[str]]:
    """`name==version` pins from a requirements file (pip-compile output too:
    hash continuation lines and `# via` comments carry no pin)."""
    found: dict[str, set[str]] = {}
    for line in text.splitlines():
        m = REQ_PIN.match(line.split("#", 1)[0].strip())
        if m:
            found.setdefault(pip_name(m.group(1)), set()).add(m.group(2))
    return found


def toml_lock_versions(text: str) -> dict[str, set[str]]:
    """`[[package]]` name/version pairs from poetry.lock or uv.lock."""
    found: dict[str, set[str]] = {}
    if tomllib is not None:
        for pkg in tomllib.loads(text).get("package") or []:
            if pkg.get("name") and pkg.get("version"):
                found.setdefault(pip_name(pkg["name"]), set()).add(str(pkg["version"]))
        return found
    name = None
    for line in text.splitlines():
        if line.strip() == "[[package]]":
            name = None
        m = re.match(r'^(name|version)\s*=\s*"([^"]+)"', line)
        if m and m.group(1) == "name":
            name = m.group(2)
        elif m and name:
            found.setdefault(pip_name(name), set()).add(m.group(2))
            name = None
    return found


def pipfile_lock_versions(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    lock = json.loads(text)
    for section in ("default", "develop"):
        for name, meta in (lock.get(section) or {}).items():
            ver = str((meta or {}).get("version") or "").lstrip("=")
            if ver:
                found.setdefault(pip_name(name), set()).add(ver)
    return found


def _candidates(ecosystem: str, manifest: str):
    """(path, reader) pairs to try for an alert, in order of preference."""
    base = posixpath.dirname(manifest)
    join = lambda n: posixpath.join(base, n) if base else n  # noqa: E731
    if ecosystem == "npm":
        return [(join("package-lock.json"), "npm"), (join("pnpm-lock.yaml"), "pnpm")]
    if ecosystem == "pip":
        if manifest.endswith(".txt"):  # the compiled requirements file is the lock
            return [(manifest, "requirements")]
        return [(join("poetry.lock"), "toml"), (join("uv.lock"), "toml"),
                (join("Pipfile.lock"), "pipfile")]
    # Never read another ecosystem's lockfile: a Python or Rust package is
    # absent from package-lock.json, and absent read as "fixed".
    return []


READERS = {
    "npm": lambda t: (npm_versions(t), npm_mismatches(t)),
    "pnpm": lambda t: (pnpm_versions(t), {}),
    "requirements": lambda t: (requirements_versions(t), {}),
    "toml": lambda t: (toml_lock_versions(t), {}),
    "pipfile": lambda t: (pipfile_lock_versions(t), {}),
}


def lockfile_versions(repo: str, ref: str, ecosystem: str, manifest: str, cache: dict):
    """Versions from the lockfile that pins an alert's manifest_path."""
    candidates = _candidates(ecosystem, manifest)
    key = tuple(candidates)
    if key not in cache:
        cache[key] = None
        for path, reader in candidates:
            text = gh_raw(repo, path, ref)
            if text is not None:
                versions, mismatched = READERS[reader](text)
                cache[key] = (path, versions, mismatched)
                break
    return cache[key]


# --- main --------------------------------------------------------------------

def check(repo: str, ref: str, only: set[int] | None) -> list[dict]:
    alerts = open_alerts(repo)
    cache: dict = {}
    rows = []
    for a in alerts:
        n = a["number"]
        if only is not None and n not in only:
            continue
        pkg = a["dependency"]["package"]["name"]
        eco = a["dependency"]["package"]["ecosystem"]
        vuln = a["security_vulnerability"]
        rng = vuln["vulnerable_version_range"]
        patched = (vuln.get("first_patched_version") or {}).get("identifier")
        row = {"alert": n, "package": pkg, "ecosystem": eco, "range": rng,
               "patched": patched, "manifest": a["dependency"]["manifest_path"]}
        got = lockfile_versions(repo, ref, eco, row["manifest"], cache)
        if got is None:
            row["status"] = "unsupported"
            row["note"] = f"no {eco} lockfile this script reads beside {row['manifest']}"
        else:
            row["lockfile"], versions, mismatched = got
            key = pip_name(pkg) if eco == "pip" else pkg
            installed = sorted(versions.get(key, ()), key=parse_version)
            bad = [v for v in installed if in_range(v, rng)]
            row["installed"] = installed
            if key in mismatched:
                row["status"] = "inconsistent"
                row["note"] = "hand-edited lockfile? " + "; ".join(mismatched[key])
            elif bad:
                row["status"] = "vulnerable"
            elif not installed:
                row["status"] = "fixed"
                row["note"] = f"no longer in {row['lockfile']}"
            elif not patched:
                # astro#20 and #21 both "fixed" GHSA-ch52-4w7c-c8xp by moving
                # http-cache-semantics to 4.3.0, the first release above the
                # advisory's `<= 4.2.0`. 4.3.0 only changed Vary handling.
                row["status"] = "unconfirmed"
                row["note"] = (f"advisory names no patched version; being outside {rng!r} "
                               "is not a fix -- cite the upstream fix or report no fix available")
            else:
                row["status"] = "fixed"
            if bad:
                row["vulnerable_versions"] = bad
        rows.append(row)

    if only is not None:
        missing = only - {r["alert"] for r in rows}
        for n in sorted(missing):
            rows.append({"alert": n, "status": "not open",
                         "note": "not in the open-alert list (already fixed or dismissed?)"})
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("repo", help="owner/repo")
    p.add_argument("ref", help="branch, tag, or SHA to read lockfiles from")
    p.add_argument("--alerts", help="comma-separated alert numbers this PR claims to fix")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    only = {int(x) for x in args.alerts.split(",") if x.strip()} if args.alerts else None
    try:
        rows = check(args.repo, args.ref, only)
    except (RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in sorted(rows, key=lambda r: (r["status"] != "vulnerable", r["alert"])):
            extra = ""
            if r["status"] == "vulnerable":
                extra = f"  installed {', '.join(r['vulnerable_versions'])} in {r['range']!r}"
            elif r["status"] in ("unsupported", "not open", "inconsistent", "unconfirmed"):
                extra = f"  {r.get('note', r.get('manifest', ''))}"
            print(f"#{r['alert']:<5} {r['status']:<12} {r.get('package', '')}{extra}")
        counts = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        if not rows:
            print(f"{args.repo}@{args.ref}: no open alerts")
        else:
            print(f"{args.repo}@{args.ref}: "
                  + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
                  + f" of {len(rows)} checked")
    return 0 if all(r["status"] == "fixed" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
