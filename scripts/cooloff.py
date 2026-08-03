#!/usr/bin/env python3
"""Resolve dependency versions subject to a publication cooloff window.

The point: a package version published minutes ago has had no time to be
noticed, reported, or yanked. Most registry-level supply chain attacks are
caught within hours. Refusing to adopt anything younger than N hours (default
24) removes the window the attacker is counting on.

Subcommands
  pkg           newest version of a registry package that clears the cooloff
  action        newest release tag of a GitHub Action, resolved to a commit SHA
  scan-actions  report every `uses:` in .github/workflows and its pin state

Every command exits non-zero if nothing clears the cooloff, so it is safe to
use in `set -e` scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

UA = "cooloff-dependency-updater/1.0 (+https://github.com/robertncl/agents)"
DEFAULT_HOURS = 24
ECOSYSTEMS = ("npm", "pypi", "crates", "rubygems", "go", "maven", "nuget")


# --------------------------------------------------------------------------
# version comparison
# --------------------------------------------------------------------------

_VERSION_RE = re.compile(
    r"^[vV]?(\d+(?:\.\d+)*)(?:[-._]?([0-9A-Za-z][0-9A-Za-z.\-]*))?(?:\+[0-9A-Za-z.\-]+)?$"
)
_PRERELEASE_WORDS = ("alpha", "beta", "rc", "dev", "pre", "canary", "next", "nightly", "snapshot")


def parse_version(raw: str):
    """Return (release_tuple, is_stable, prerelease_key) or None if unparseable."""
    m = _VERSION_RE.match(raw.strip())
    if not m:
        return None
    release = tuple(int(p) for p in m.group(1).split("."))
    suffix = m.group(2)
    if suffix is None:
        return (release, 1, ())
    low = suffix.lower()
    # A trailing suffix is only a prerelease if it looks like one. Things like
    # `1.0.0.Final` or `2.0.0-RELEASE` are stable despite having a suffix.
    if any(w in low for w in _PRERELEASE_WORDS):
        return (release, 0, _suffix_key(suffix))
    return (release, 1, _suffix_key(suffix))


def _suffix_key(suffix: str):
    parts = re.split(r"[.\-_]", suffix)
    key = []
    for p in parts:
        if p.isdigit():
            key.append((1, int(p), ""))
        else:
            key.append((0, 0, p.lower()))
    return tuple(key)


def sort_key(raw: str):
    parsed = parse_version(raw)
    if parsed is None:
        return ((-1,), 0, ())
    # Pad release tuples so 1.2 and 1.2.0 compare equal-ish and 1.10 > 1.9.
    release, stable, pre = parsed
    padded = release + (0,) * (6 - len(release)) if len(release) < 6 else release
    return (padded, stable, pre)


def major_of(raw: str):
    parsed = parse_version(raw)
    return parsed[0][0] if parsed else None


def is_stable(raw: str) -> bool:
    parsed = parse_version(raw)
    return bool(parsed and parsed[1] == 1)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    # Trim fractional seconds beyond microseconds, which fromisoformat rejects.
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_hours(dt: datetime) -> float:
    return (now() - dt).total_seconds() / 3600.0


def fetch_json(url: str, accept: str = "application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------
# registry adapters -> list of (version, published_datetime)
# --------------------------------------------------------------------------


def versions_npm(name: str):
    url = f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}"
    data = fetch_json(url)
    times = data.get("time", {})
    out = []
    for ver, meta in (data.get("versions") or {}).items():
        if isinstance(meta, dict) and meta.get("deprecated"):
            continue
        ts = parse_ts(times.get(ver, ""))
        if ts:
            out.append((ver, ts))
    return out


def versions_pypi(name: str):
    data = fetch_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    out = []
    for ver, files in (data.get("releases") or {}).items():
        stamps = [parse_ts(f.get("upload_time_iso_8601", "")) for f in files or []]
        stamps = [s for s in stamps if s]
        if not stamps:
            continue
        if all(f.get("yanked") for f in files):
            continue
        out.append((ver, min(stamps)))
    return out


def versions_crates(name: str):
    data = fetch_json(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}/versions")
    out = []
    for v in data.get("versions") or []:
        if v.get("yanked"):
            continue
        ts = parse_ts(v.get("created_at", ""))
        if ts:
            out.append((v["num"], ts))
    return out


def versions_rubygems(name: str):
    data = fetch_json(f"https://rubygems.org/api/v1/versions/{urllib.parse.quote(name)}.json")
    out = []
    for v in data:
        ts = parse_ts(v.get("created_at", ""))
        if ts:
            out.append((v["number"], ts))
    return out


def _go_escape(path: str) -> str:
    return re.sub(r"([A-Z])", lambda m: "!" + m.group(1).lower(), path)


def versions_go(name: str):
    base = f"https://proxy.golang.org/{_go_escape(name)}"
    listing = fetch_text(f"{base}/@v/list")
    out = []
    for ver in filter(None, (l.strip() for l in listing.splitlines())):
        try:
            info = fetch_json(f"{base}/@v/{_go_escape(ver)}.info")
        except urllib.error.HTTPError:
            continue
        ts = parse_ts(info.get("Time", ""))
        if ts:
            out.append((ver, ts))
    return out


def versions_maven(name: str):
    if ":" not in name:
        die("maven packages must be given as group:artifact")
    group, artifact = name.split(":", 1)
    q = urllib.parse.quote(f'g:"{group}" AND a:"{artifact}"')
    data = fetch_json(
        f"https://search.maven.org/solrsearch/select?q={q}&core=gav&rows=200&wt=json"
    )
    out = []
    for doc in data.get("response", {}).get("docs") or []:
        ts = doc.get("timestamp")
        if ts:
            out.append((doc["v"], datetime.fromtimestamp(ts / 1000, tz=timezone.utc)))
    return out


def versions_nuget(name: str):
    lower = urllib.parse.quote(name.lower())
    data = fetch_json(f"https://api.nuget.org/v3/registration5-semver1/{lower}/index.json")
    out = []
    for page in data.get("items") or []:
        items = page.get("items")
        if items is None:
            items = (fetch_json(page["@id"]) or {}).get("items") or []
        for it in items:
            entry = it.get("catalogEntry") or {}
            ver = entry.get("version")
            ts = parse_ts(entry.get("published", ""))
            # NuGet marks unlisted packages with the year 1900.
            if ver and ts and ts.year > 1900:
                out.append((ver, ts))
    return out


FETCHERS = {
    "npm": versions_npm,
    "pypi": versions_pypi,
    "crates": versions_crates,
    "rubygems": versions_rubygems,
    "go": versions_go,
    "maven": versions_maven,
    "nuget": versions_nuget,
}


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def select(candidates, hours, allow_prerelease, same_major_as=None):
    """Newest candidate clearing the cooloff. Returns (chosen, skipped, considered)."""
    considered = []
    for ver, ts in candidates:
        if parse_version(ver) is None:
            continue
        if not allow_prerelease and not is_stable(ver):
            continue
        if same_major_as is not None and major_of(ver) != same_major_as:
            continue
        considered.append((ver, ts))
    considered.sort(key=lambda p: sort_key(p[0]), reverse=True)

    skipped = []
    for ver, ts in considered:
        age = age_hours(ts)
        if age < hours:
            skipped.append({"version": ver, "published": ts.isoformat(), "age_hours": round(age, 2)})
            continue
        return (
            {"version": ver, "published": ts.isoformat(), "age_hours": round(age, 2)},
            skipped,
            considered,
        )
    return (None, skipped, considered)


# --------------------------------------------------------------------------
# github actions
# --------------------------------------------------------------------------


def gh_api(path: str, paginate: bool = False):
    cmd = ["gh", "api", path]
    if paginate:
        cmd.insert(2, "--paginate")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh api {path} failed")
    text = proc.stdout.strip()
    if not text:
        return None
    # --paginate concatenates JSON arrays; stitch them back together.
    if paginate and "][" in text:
        text = text.replace("][", ",")
    return json.loads(text)


def action_tag_dates(repo: str):
    """Map tag -> most conservative (latest) known publication timestamp."""
    dates: dict[str, datetime] = {}

    try:
        releases = gh_api(f"repos/{repo}/releases?per_page=100", paginate=True) or []
    except RuntimeError:
        releases = []
    for rel in releases:
        if rel.get("draft"):
            continue
        ts = parse_ts(rel.get("published_at") or rel.get("created_at") or "")
        tag = rel.get("tag_name")
        if tag and ts:
            dates[tag] = ts

    tags = gh_api(f"repos/{repo}/tags?per_page=100", paginate=True) or []
    return dates, [t["name"] for t in tags]


def resolve_commit(repo: str, tag: str):
    """Return (sha, conservative_date) for a tag, dereferencing annotated tags."""
    commit = gh_api(f"repos/{repo}/commits/{urllib.parse.quote(tag)}")
    sha = commit["sha"]
    committed = parse_ts(commit["commit"]["committer"]["date"]) or now()

    dates = [committed]
    # An annotated tag carries its own creation date, which is the moment the
    # release actually became reachable. A tag can point at an old commit, so
    # take the latest signal, never the earliest.
    try:
        ref = gh_api(f"repos/{repo}/git/ref/tags/{urllib.parse.quote(tag)}")
        obj = (ref or {}).get("object") or {}
        if obj.get("type") == "tag":
            tag_obj = gh_api(f"repos/{repo}/git/tags/{obj['sha']}")
            ts = parse_ts(((tag_obj or {}).get("tagger") or {}).get("date", ""))
            if ts:
                dates.append(ts)
    except RuntimeError:
        pass
    return sha, max(dates)


def cmd_action(args):
    ref = args.ref
    repo, _, current = ref.partition("@")
    parts = repo.strip("/").split("/")
    if len(parts) < 2:
        die("action must be given as owner/repo[/subdir][@ref]")
    repo = "/".join(parts[:2])

    release_dates, tags = action_tag_dates(repo)
    same_major = major_of(current) if args.same_major and current else None

    usable = []
    for t in tags:
        if parse_version(t) is None:
            continue
        if not args.allow_prerelease and not is_stable(t):
            continue
        if same_major is not None and major_of(t) != same_major:
            continue
        # Skip floating major/minor aliases (v4, v4.2) when a full version
        # exists -- they move, which defeats the point of pinning to a SHA.
        usable.append(t)
    if not usable:
        die(f"no usable version tags found for {repo}")

    specific = [t for t in usable if len(parse_version(t)[0]) >= 3]
    usable = specific or usable
    usable.sort(key=sort_key, reverse=True)

    skipped = []
    for tag in usable[: args.max_probe]:
        sha, tag_date = resolve_commit(repo, tag)
        published = max([tag_date] + ([release_dates[tag]] if tag in release_dates else []))
        age = age_hours(published)
        if age < args.hours:
            skipped.append({"tag": tag, "published": published.isoformat(), "age_hours": round(age, 2)})
            continue
        result = {
            "repo": repo,
            "current": current or None,
            "tag": tag,
            "sha": sha,
            "published": published.isoformat(),
            "age_hours": round(age, 2),
            "cooloff_hours": args.hours,
            "uses": f"{ref.split('@')[0]}@{sha} # {tag}",
            "skipped_too_new": skipped,
        }
        emit(result, args.json, lambda r: r["uses"])
        return
    die(
        f"no release of {repo} is older than {args.hours}h "
        f"(checked {len(usable[: args.max_probe])} tags, newest held back: "
        f"{', '.join(s['tag'] for s in skipped) or 'none'})"
    )


# --------------------------------------------------------------------------
# workflow scanning
# --------------------------------------------------------------------------

_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^'\"#\s]+)['\"]?\s*(#.*)?$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def cmd_scan_actions(args):
    root = os.path.abspath(args.dir)
    wf_dir = os.path.join(root, ".github", "workflows")
    targets = []
    for base in (wf_dir, os.path.join(root, ".github", "actions")):
        for dirpath, _, files in os.walk(base):
            for f in files:
                if f.endswith((".yml", ".yaml")):
                    targets.append(os.path.join(dirpath, f))

    findings = []
    for path in sorted(targets):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                m = _USES_RE.match(line.rstrip("\n"))
                if not m:
                    continue
                uses = m.group(1)
                if uses.startswith(("./", "docker://")):
                    continue
                _, _, ref = uses.partition("@")
                findings.append(
                    {
                        "file": os.path.relpath(path, root),
                        "line": lineno,
                        "uses": uses,
                        "ref": ref or None,
                        "pinned": bool(ref and _SHA_RE.match(ref)),
                        "comment": (m.group(2) or "").lstrip("# ").strip() or None,
                    }
                )

    if args.json:
        print(json.dumps(findings, indent=2))
        return
    if not findings:
        print("no external action references found")
        return
    unpinned = [f for f in findings if not f["pinned"]]
    for f in findings:
        mark = "OK  " if f["pinned"] else "PIN!"
        note = f"  ({f['comment']})" if f["comment"] else ""
        print(f"{mark} {f['file']}:{f['line']}  {f['uses']}{note}")
    print(f"\n{len(findings)} references, {len(unpinned)} not pinned to a SHA")
    if unpinned:
        sys.exit(2)


# --------------------------------------------------------------------------
# package command
# --------------------------------------------------------------------------


def emit(result, as_json, plain):
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        print(plain(result))


def cmd_pkg(args):
    fetcher = FETCHERS[args.ecosystem]
    try:
        candidates = fetcher(args.name)
    except urllib.error.HTTPError as e:
        die(f"{args.ecosystem}:{args.name}: HTTP {e.code}")
    except urllib.error.URLError as e:
        die(f"{args.ecosystem}:{args.name}: {e.reason}")
    if not candidates:
        die(f"no published versions found for {args.ecosystem}:{args.name}")

    same_major = major_of(args.current) if args.same_major and args.current else None
    chosen, skipped, considered = select(candidates, args.hours, args.allow_prerelease, same_major)
    if not chosen:
        die(
            f"{args.name}: no version older than {args.hours}h "
            f"(held back: {', '.join(s['version'] for s in skipped) or 'none'})"
        )

    result = {
        "ecosystem": args.ecosystem,
        "name": args.name,
        "current": args.current,
        "target": chosen["version"],
        "published": chosen["published"],
        "age_hours": chosen["age_hours"],
        "cooloff_hours": args.hours,
        "changed": bool(args.current) and args.current.lstrip("^~=v ") != chosen["version"],
        "skipped_too_new": skipped,
        "versions_considered": len(considered),
    }
    emit(result, args.json, lambda r: r["target"])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--hours", type=float, default=float(os.environ.get("COOLOFF_HOURS", DEFAULT_HOURS)),
                        help=f"minimum age in hours (default {DEFAULT_HOURS}, or $COOLOFF_HOURS)")
    common.add_argument("--allow-prerelease", action="store_true")
    common.add_argument("--same-major", action="store_true", help="do not cross a major version")
    common.add_argument("--json", action="store_true")

    sp = sub.add_parser("pkg", parents=[common], help="resolve a registry package version")
    sp.add_argument("--ecosystem", "-e", required=True, choices=ECOSYSTEMS)
    sp.add_argument("--name", "-n", required=True)
    sp.add_argument("--current", "-c", default=None)
    sp.set_defaults(func=cmd_pkg)

    sa = sub.add_parser("action", parents=[common], help="resolve a GitHub Action to a SHA")
    sa.add_argument("ref", help="owner/repo[@current-ref]")
    sa.add_argument("--max-probe", type=int, default=10, help="how many recent tags to inspect")
    sa.set_defaults(func=cmd_action)

    ss = sub.add_parser("scan-actions", help="report pin state of every workflow `uses:`")
    ss.add_argument("--dir", default=".")
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=cmd_scan_actions)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
