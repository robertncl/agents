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
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
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
# PEP 440 spells prereleases as bare letters -- 2.14.0a1, 2.0.0b2, 1.5c3 -- which
# no keyword above would catch. Without this, an alpha reads as stable and
# slips into a sweep that was supposed to exclude prereleases.
_PRERELEASE_RE = re.compile(r"^[._-]?(?:a|b|c|rc|alpha|beta|pre|preview|dev)[._-]?\d*$", re.I)


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
    if any(w in low for w in _PRERELEASE_WORDS) or _PRERELEASE_RE.match(low):
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


# A single batch run routinely asks for the same registry document more than
# once -- the same package pinned in two manifests, or the same action used in
# five workflows. Caching by URL keeps that to one request. Entries are held
# for the life of the process only, so nothing goes stale across runs.
_CACHE: dict[str, object] = {}
_CACHE_LOCK = threading.Lock()


def _cached(url: str, produce):
    with _CACHE_LOCK:
        if url in _CACHE:
            hit = _CACHE[url]
            if isinstance(hit, Exception):
                raise hit
            return hit
    try:
        value = produce()
    except (urllib.error.URLError, RuntimeError) as e:
        # Cache the failure too: re-requesting a 404'd package once per
        # manifest that mentions it is pure latency.
        with _CACHE_LOCK:
            _CACHE[url] = e
        raise
    with _CACHE_LOCK:
        _CACHE[url] = value
    return value


def fetch_json(url: str, accept: str = "application/json"):
    def go():
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return _cached(url, go)


def fetch_text(url: str) -> str:
    def go():
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")

    return _cached("text:" + url, go)


class ResolveError(Exception):
    """A single spec could not be resolved. Fatal for `pkg`/`action`, but only
    one row of the report for `batch`."""

    def __init__(self, msg, held_back=None):
        super().__init__(msg)
        self.held_back = held_back or []


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


def versions_go(name: str, probe: int = 20):
    base = f"https://proxy.golang.org/{_go_escape(name)}"
    listing = fetch_text(f"{base}/@v/list")
    listed = [v for v in (l.strip() for l in listing.splitlines()) if v]
    # The proxy has no bulk date endpoint -- one request per version. Only the
    # newest handful can ever win selection, so sort first and probe the top
    # slice instead of walking the entire release history.
    listed = [v for v in listed if parse_version(v) is not None]
    listed.sort(key=sort_key, reverse=True)
    out = []
    for ver in listed[:probe]:
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
    def go():
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

    # Repos repeat across workflows; the tag/release listing is the expensive
    # call, so cache it rather than re-shelling out to `gh` for every `uses:`.
    return _cached(f"gh:{'paginate:' if paginate else ''}{path}", go)


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


def resolve_action(ref, hours, allow_prerelease=False, same_major_only=False, max_probe=10):
    repo, _, current = ref.partition("@")
    parts = repo.strip("/").split("/")
    if len(parts) < 2:
        raise ResolveError("action must be given as owner/repo[/subdir][@ref]")
    repo = "/".join(parts[:2])

    release_dates, tags = action_tag_dates(repo)
    same_major = major_of(current) if same_major_only and current else None

    usable = []
    for t in tags:
        if parse_version(t) is None:
            continue
        if not allow_prerelease and not is_stable(t):
            continue
        if same_major is not None and major_of(t) != same_major:
            continue
        # Skip floating major/minor aliases (v4, v4.2) when a full version
        # exists -- they move, which defeats the point of pinning to a SHA.
        usable.append(t)
    if not usable:
        raise ResolveError(f"no usable version tags found for {repo}")

    specific = [t for t in usable if len(parse_version(t)[0]) >= 3]
    usable = specific or usable
    usable.sort(key=sort_key, reverse=True)

    skipped = []
    for tag in usable[:max_probe]:
        sha, tag_date = resolve_commit(repo, tag)
        published = max([tag_date] + ([release_dates[tag]] if tag in release_dates else []))
        age = age_hours(published)
        if age < hours:
            skipped.append({"tag": tag, "published": published.isoformat(), "age_hours": round(age, 2)})
            continue
        return {
            "kind": "action",
            "repo": repo,
            "current": current or None,
            "tag": tag,
            "sha": sha,
            "published": published.isoformat(),
            "age_hours": round(age, 2),
            "cooloff_hours": hours,
            "uses": f"{ref.split('@')[0]}@{sha} # {tag}",
            "changed": current != sha,
            "skipped_too_new": skipped,
        }
    raise ResolveError(
        f"no release of {repo} is older than {hours}h "
        f"(checked {len(usable[:max_probe])} tags, newest held back: "
        f"{', '.join(s['tag'] for s in skipped) or 'none'})",
        held_back=skipped,
    )


def cmd_action(args):
    try:
        result = resolve_action(
            args.ref, args.hours, args.allow_prerelease, args.same_major, args.max_probe
        )
    except ResolveError as e:
        die(str(e))
    emit(result, args.json, lambda r: r["uses"])


# --------------------------------------------------------------------------
# workflow scanning
# --------------------------------------------------------------------------

_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^'\"#\s]+)['\"]?\s*(#.*)?$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def collect_uses(root: str):
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
    return findings


def cmd_scan_actions(args):
    root = os.path.abspath(args.dir)
    findings = collect_uses(root)

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


def resolve_pkg(ecosystem, name, current=None, hours=DEFAULT_HOURS,
                allow_prerelease=False, same_major_only=False):
    fetcher = FETCHERS[ecosystem]
    try:
        candidates = fetcher(name)
    except urllib.error.HTTPError as e:
        raise ResolveError(f"{ecosystem}:{name}: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise ResolveError(f"{ecosystem}:{name}: {e.reason}")
    except SystemExit as e:  # maven's group:artifact guard
        raise ResolveError(f"{ecosystem}:{name}: bad package spec") from e
    if not candidates:
        raise ResolveError(f"no published versions found for {ecosystem}:{name}")

    same_major = major_of(current) if same_major_only and current else None
    chosen, skipped, considered = select(candidates, hours, allow_prerelease, same_major)
    if not chosen:
        head = ", ".join(s["version"] for s in skipped[:5]) or "none"
        more = f" (+{len(skipped) - 5} older)" if len(skipped) > 5 else ""
        raise ResolveError(
            f"{name}: no version older than {hours}h (held back: {head}{more})",
            held_back=skipped,
        )

    return {
        "kind": "pkg",
        "ecosystem": ecosystem,
        "name": name,
        "current": current,
        "target": chosen["version"],
        "published": chosen["published"],
        "age_hours": chosen["age_hours"],
        "cooloff_hours": hours,
        "changed": bool(current) and current.lstrip("^~>=<= v") != chosen["version"],
        "skipped_too_new": skipped,
        "versions_considered": len(considered),
    }


def cmd_pkg(args):
    try:
        result = resolve_pkg(
            args.ecosystem, args.name, args.current, args.hours,
            args.allow_prerelease, args.same_major,
        )
    except ResolveError as e:
        die(str(e))
    emit(result, args.json, lambda r: r["target"])


# --------------------------------------------------------------------------
# manifest discovery
# --------------------------------------------------------------------------

# Values that name a version we can resolve, versus values that point somewhere
# else entirely (a path, a git URL, a workspace sibling). The latter have no
# registry publication date, so there is nothing to cool off.
_UNRESOLVABLE = ("file:", "link:", "workspace:", "git+", "git:", "http:", "https:",
                 "portal:", "patch:", "npm:", "catalog:")
_SKIP_DIRS = {".git", "node_modules", "vendor", "target", "dist", "build",
              ".venv", "venv", "__pycache__", ".tox", ".mypy_cache"}


def _walk(root: str):
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in files:
            yield dirpath, f


def _rel(root, path):
    return os.path.relpath(path, root)


def _clean_version(raw):
    """Strip range operators to get something usable as `current`."""
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw or raw in ("*", "latest", "x") or raw.startswith(_UNRESOLVABLE):
        return None
    m = re.search(r"\d[\w.\-+]*", raw)
    return m.group(0) if m else None


def _load_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        return None
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return None


def _deps_package_json(path, root, out, notes):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        notes.append(f"{_rel(root, path)}: unreadable ({e})")
        return
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, spec in (data.get(section) or {}).items():
            if isinstance(spec, str) and spec.startswith(_UNRESOLVABLE):
                continue
            out.append({"ecosystem": "npm", "name": name,
                        "current": _clean_version(spec), "file": _rel(root, path)})


def _deps_requirements(path, root, out, notes):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-") or "://" in line:
                continue
            m = re.match(r"^([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*(.*)$", line)
            if not m:
                continue
            out.append({"ecosystem": "pypi", "name": m.group(1),
                        "current": _clean_version(m.group(2)), "file": _rel(root, path)})


def _deps_pyproject(path, root, out, notes):
    data = _load_toml(path)
    if data is None:
        notes.append(f"{_rel(root, path)}: needs Python 3.11+ (tomllib) to parse — check by hand")
        return
    specs = list((data.get("project") or {}).get("dependencies") or [])
    for group in ((data.get("project") or {}).get("optional-dependencies") or {}).values():
        specs.extend(group or [])
    for spec in specs:
        m = re.match(r"^([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*(.*)$", str(spec).strip())
        if m:
            out.append({"ecosystem": "pypi", "name": m.group(1),
                        "current": _clean_version(m.group(2)), "file": _rel(root, path)})
    poetry = ((data.get("tool") or {}).get("poetry") or {})
    for section in ("dependencies", "dev-dependencies"):
        for name, spec in (poetry.get(section) or {}).items():
            if name.lower() == "python":
                continue
            if isinstance(spec, dict):
                spec = spec.get("version")
            out.append({"ecosystem": "pypi", "name": name,
                        "current": _clean_version(spec), "file": _rel(root, path)})


def _deps_cargo(path, root, out, notes):
    data = _load_toml(path)
    if data is None:
        notes.append(f"{_rel(root, path)}: needs Python 3.11+ (tomllib) to parse — check by hand")
        return
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name, spec in (data.get(section) or {}).items():
            if isinstance(spec, dict):
                if spec.get("path") or spec.get("git"):
                    continue
                spec = spec.get("version")
            out.append({"ecosystem": "crates", "name": name,
                        "current": _clean_version(spec), "file": _rel(root, path)})


def _deps_gomod(path, root, out, notes):
    text = open(path, encoding="utf-8", errors="replace").read()
    # Both `require (...)` blocks and single-line `require x v1.2.3`.
    for m in re.finditer(r"^\s*(?:require\s+)?([\w.\-]+\.[\w.\-/~]+)\s+(v[\w.\-+]+)", text, re.M):
        if "// indirect" in text[m.start():text.find("\n", m.end()) + 1]:
            continue
        out.append({"ecosystem": "go", "name": m.group(1),
                    "current": m.group(2), "file": _rel(root, path)})


def _deps_gemfile(path, root, out, notes):
    text = open(path, encoding="utf-8", errors="replace").read()
    for m in re.finditer(r"^\s*gem\s+['\"]([^'\"]+)['\"]\s*(?:,\s*['\"]([^'\"]+)['\"])?", text, re.M):
        out.append({"ecosystem": "rubygems", "name": m.group(1),
                    "current": _clean_version(m.group(2)), "file": _rel(root, path)})


def _deps_csproj(path, root, out, notes):
    text = open(path, encoding="utf-8", errors="replace").read()
    for tag in re.findall(r"<PackageReference\b[^>]*>", text):
        name = re.search(r"Include=\"([^\"]+)\"", tag)
        # Version can also be a child element or come from central package
        # management; a missing one just means we resolve without a baseline.
        version = re.search(r"Version=\"([^\"]+)\"", tag)
        if name:
            out.append({"ecosystem": "nuget", "name": name.group(1),
                        "current": _clean_version(version.group(1) if version else None),
                        "file": _rel(root, path)})


def _deps_pom(path, root, out, notes):
    text = open(path, encoding="utf-8", errors="replace").read()
    for block in re.findall(r"<dependency>(.*?)</dependency>", text, re.S):
        g = re.search(r"<groupId>([^<]+)</groupId>", block)
        a = re.search(r"<artifactId>([^<]+)</artifactId>", block)
        v = re.search(r"<version>([^<]+)</version>", block)
        if not (g and a):
            continue
        version = v.group(1).strip() if v else None
        if version and version.startswith("${"):
            # Resolved from a <properties> block; report it rather than
            # guessing at the indirection.
            notes.append(f"{_rel(root, path)}: {g.group(1)}:{a.group(1)} version is a property ({version})")
            version = None
        out.append({"ecosystem": "maven", "name": f"{g.group(1).strip()}:{a.group(1).strip()}",
                    "current": _clean_version(version), "file": _rel(root, path)})


_MANIFESTS = [
    (lambda f: f == "package.json", _deps_package_json),
    (lambda f: f.startswith("requirements") and f.endswith(".txt"), _deps_requirements),
    (lambda f: f == "pyproject.toml", _deps_pyproject),
    (lambda f: f == "Cargo.toml", _deps_cargo),
    (lambda f: f == "go.mod", _deps_gomod),
    (lambda f: f in ("Gemfile", "gems.rb"), _deps_gemfile),
    (lambda f: f.endswith((".csproj", ".fsproj", ".vbproj")), _deps_csproj),
    (lambda f: f == "pom.xml", _deps_pom),
]


def scan_deps(root: str, include_actions: bool = True):
    """Every resolvable dependency in the tree, deduplicated."""
    found, notes = [], []
    for dirpath, fname in _walk(root):
        for matches, parser in _MANIFESTS:
            if matches(fname):
                path = os.path.join(dirpath, fname)
                try:
                    parser(path, root, found, notes)
                except Exception as e:
                    notes.append(f"{_rel(root, path)}: parse failed ({e})")
                break

    if include_actions:
        for f in collect_uses(root):
            repo = "/".join(f["uses"].split("@")[0].strip("/").split("/")[:2])
            found.append({"ecosystem": "action", "name": repo,
                          "current": f["ref"], "file": f["file"], "line": f["line"],
                          "pinned": f["pinned"], "comment": f["comment"]})

    # The same package pinned in three manifests is one resolution, not three.
    seen, deduped = {}, []
    for d in found:
        # Actions already pinned to a SHA carry their tag in the comment; that
        # is the version to compare against, not the SHA.
        key = (d["ecosystem"], d["name"])
        if key in seen:
            seen[key]["files"].append(d.get("file"))
            continue
        entry = dict(d)
        entry["files"] = [d.get("file")]
        entry.pop("file", None)
        seen[key] = entry
        deduped.append(entry)
    return deduped, notes


def spec_of(dep):
    if dep["ecosystem"] == "action":
        cur = dep.get("comment") if dep.get("pinned") else dep.get("current")
        return f"action:{dep['name']}" + (f"@{cur}" if cur else "")
    cur = dep.get("current")
    return f"{dep['ecosystem']}:{dep['name']}" + (f"@{cur}" if cur else "")


def cmd_scan_deps(args):
    root = os.path.abspath(args.dir)
    deps, notes = scan_deps(root, include_actions=not args.no_actions)
    if args.json:
        print(json.dumps({"dependencies": deps, "notes": notes}, indent=2))
        return
    for d in deps:
        print(spec_of(d))
    for n in notes:
        print(f"note: {n}", file=sys.stderr)
    if not deps:
        print("no manifests with resolvable dependencies found", file=sys.stderr)


# --------------------------------------------------------------------------
# batch resolution
# --------------------------------------------------------------------------


def parse_spec(spec: str):
    """`npm:react@^18.2.0`, `pypi:requests`, `action:actions/checkout@v4`."""
    spec = spec.strip()
    eco, sep, rest = spec.partition(":")
    if not sep or not rest:
        raise ValueError(f"bad spec {spec!r} — expected ecosystem:name[@current]")
    if eco not in ECOSYSTEMS and eco != "action":
        raise ValueError(f"bad spec {spec!r} — unknown ecosystem {eco!r}")
    if eco == "action":
        return eco, rest, None
    name, at, current = rest.rpartition("@")
    if not at:
        return eco, rest, None
    return eco, name, current or None


def resolve_spec(spec, hours, allow_prerelease, same_major, max_probe):
    try:
        eco, name, current = parse_spec(spec)
    except ValueError as e:
        return {"spec": spec, "status": "error", "error": str(e)}
    try:
        if eco == "action":
            r = resolve_action(name, hours, allow_prerelease, same_major, max_probe)
        else:
            r = resolve_pkg(eco, name, current, hours, allow_prerelease, same_major)
    except ResolveError as e:
        return {
            "spec": spec,
            "status": "held_back" if e.held_back else "error",
            "error": str(e),
            "skipped_too_new": e.held_back,
        }
    except Exception as e:  # one bad package must not sink the whole sweep
        return {"spec": spec, "status": "error", "error": f"{type(e).__name__}: {e}"}
    r["spec"] = spec
    if not r.get("current"):
        # Nothing to compare against (a floating `gem "puma"`, an unpinned
        # PackageReference). Report the resolved target, don't claim it matches.
        r["status"] = "resolved"
    else:
        r["status"] = "update" if r.get("changed") else "current"
    return r


def cmd_batch(args):
    specs = list(args.specs)
    if not specs or specs == ["-"]:
        specs = [l.strip() for l in sys.stdin.read().splitlines()]
    specs = [s for s in specs if s and not s.startswith("#")]
    if not specs:
        die("no specs given (pass them as arguments or on stdin)")

    # Registry calls are almost entirely network wait, so they overlap well.
    # Keep the pool modest: these are public registries and `gh` subprocesses.
    results = [None] * len(specs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(resolve_spec, s, args.hours, args.allow_prerelease,
                        args.same_major, args.max_probe): i
            for i, s in enumerate(specs)
        }
        for fut in concurrent.futures.as_completed(futures):
            results[futures[fut]] = fut.result()

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        width = max(len(r["spec"]) for r in results)
        for r in results:
            if r["status"] in ("update", "resolved"):
                target = f"{r['tag']} ({r['sha'][:12]}…)" if r.get("sha") else r.get("target")
                detail = f"-> {target}  [{r['age_hours']:.0f}h old]"
                if r["status"] == "resolved":
                    detail += "  (no version pinned in source)"
            elif r["status"] == "current":
                detail = "up to date"
            else:
                detail = r["error"]
            print(f"{r['status'].upper():<10} {r['spec']:<{width}}  {detail}")
        counts = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        print("\n" + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())), file=sys.stderr)

    if any(r["status"] == "error" for r in results):
        sys.exit(3)


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

    sd = sub.add_parser("scan-deps", help="list every dependency in the tree as a batch spec")
    sd.add_argument("--dir", default=".")
    sd.add_argument("--no-actions", action="store_true", help="manifests only, skip workflow `uses:`")
    sd.add_argument("--json", action="store_true", help="full inventory with source files")
    sd.set_defaults(func=cmd_scan_deps)

    sb = sub.add_parser("batch", parents=[common],
                        help="resolve many specs concurrently (args or stdin)")
    sb.add_argument("specs", nargs="*", help="ecosystem:name[@current], or `-` for stdin")
    sb.add_argument("--jobs", "-j", type=int, default=8, help="concurrent resolutions (default 8)")
    sb.add_argument("--max-probe", type=int, default=10, help="tags to inspect per action")
    sb.set_defaults(func=cmd_batch)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
