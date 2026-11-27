#!/usr/bin/env python3
"""Download each finding's upstream source at its recorded commit, for the reviewer to read.

Usage:
    python rules/scripts/fetch_sources.py --context context --tools-dir rules/data/tools --out upstream

For every finding in context/findings.json, the source must sit under the
tool's repo or official_sources as main records them. It is then fetched at
source_commit and saved as upstream/<tool>/<commit>/<path>:

    https://github.com/O/R/blob/<ref>/<path>   raw file <path> at the commit
    https://github.com/O/R#<anchor>            the README GitHub shows there (README.md) at the commit
    https://github.com/O/R/releases/tag/<tag>  the release notes, if the tag points at the commit

A removal (a finding whose entry becomes delisted) is also fetched at its
removed_commit, the upstream commit where the row is gone, and that commit's
date is recorded, so the reviewer can confirm the row is no longer listed.

Only raw.githubusercontent.com and api.github.com are contacted. Files over
2 MB are skipped. upstream/index.json maps each finding id to its file, or
to the reason it is missing (and, for a removal, to removed_file and
removed_date, or removed_missing); the reviewer marks missing sources
`ambiguous`.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ghapi  # noqa: E402
from common import load_yaml  # noqa: E402
from validate import is_under  # noqa: E402

MAX_BYTES = 2 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}")
SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
BLOB = re.compile(r"https://github\.com/([^/]+)/([^/#?]+)/blob/[^/]+/([^#?]+)(?:[#?].*)?")
README = re.compile(r"https://github\.com/([^/]+)/([^/#?]+)/?(?:#.*)?")
RELEASE = re.compile(r"https://github\.com/([^/]+)/([^/#?]+)/releases/tag/([^/#?]+)")


def safe_path(path):
    parts = path.split("/")
    return all(SEGMENT.fullmatch(p) and p not in (".", "..") for p in parts)


def locate(source, commit):
    """(kind, owner, repo, path_or_tag) for a source URL, or raise ValueError."""
    if not SHA.fullmatch(commit or ""):
        raise ValueError("no full source_commit")
    for pattern, kind in ((BLOB, "file"), (RELEASE, "release"), (README, "readme")):
        match = pattern.fullmatch(source)
        if match:
            owner, repo = match.group(1), match.group(2)
            target = "README.md" if kind == "readme" else match.group(3)
            if not (SEGMENT.fullmatch(owner) and SEGMENT.fullmatch(repo) and safe_path(target)):
                raise ValueError("unsafe characters in source URL")
            return ("file" if kind == "readme" else kind), owner, repo, target
    raise ValueError("source is not a GitHub file, README or release")


def download(url):
    request = urllib.request.Request(url, headers={"User-Agent": "k8s-m8s-review"})
    with urllib.request.urlopen(request, timeout=60) as response:
        if int(response.headers.get("Content-Length") or 0) > MAX_BYTES:
            raise ValueError("over 2 MB")
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("over 2 MB")
    return data


def fetch(item, allowed, out):
    """Save one finding's source. Returns its index entry."""
    source, commit = item.get("source") or "", item.get("source_commit")
    if not any(is_under(source, prefix) for prefix in allowed):
        return {"missing": "source is not under the tool's repo or official_sources"}
    try:
        kind, owner, repo, target = locate(source, commit)
    except ValueError as exc:
        return {"missing": str(exc)}
    tool = item["tool"]
    try:
        if kind == "file":
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{target}"
            data, name = download(url), target
        else:
            tagged = ghapi.get(f"repos/{owner}/{repo}/commits/{ghapi.quote(target)}")["sha"]
            if tagged != commit:
                return {"missing": f"tag {target} points at {tagged[:7]}, not the recorded commit"}
            release = ghapi.get(f"repos/{owner}/{repo}/releases/tags/{ghapi.quote(target)}")
            url = release["html_url"]
            data, name = (release.get("body") or "").encode("utf-8")[:MAX_BYTES], f"release-{target}.md"
    except (urllib.error.URLError, ghapi.GitHubError, ValueError) as exc:
        return {"missing": f"could not fetch: {exc}"}
    path = Path(out) / tool / commit / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"file": path.as_posix(), "url": url}


def is_removal(item):
    return "delisted" in (item.get("new") or {}) and "delisted" not in (item.get("old") or {})


def fetch_removed(item, allowed, out):
    """The source at the commit where the row is gone, and that commit's date."""
    commit = item.get("removed_commit")
    if not SHA.fullmatch(commit or ""):
        return {"removed_missing": "no upstream commit showing the removal"}
    entry = fetch({**item, "source_commit": commit}, allowed, out)
    if "missing" in entry:
        return {"removed_missing": entry["missing"]}
    _, owner, repo, _ = locate(item["source"], commit)
    try:
        date = ghapi.get(f"repos/{owner}/{repo}/commits/{commit}")["commit"]["committer"]["date"][:10]
    except (urllib.error.URLError, ghapi.GitHubError, KeyError, TypeError) as exc:
        return {"removed_missing": f"could not read the commit: {exc}"}
    return {"removed_file": entry["file"], "removed_url": entry["url"], "removed_date": date}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--context", required=True)
    parser.add_argument("--tools-dir", required=True, help="data/tools from main, for the source allowlist")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    items = json.loads((Path(args.context) / "findings.json").read_text(encoding="utf-8"))
    index = {}
    for item in items:
        tool_file = Path(args.tools_dir) / f"{item['tool']}.yaml"
        if not SEGMENT.fullmatch(item["tool"]) or not tool_file.is_file():
            index[item["id"]] = {"missing": "no data file for this tool on main"}
            continue
        data = load_yaml(tool_file)
        allowed = [data["repo"], *data.get("official_sources", [])]
        index[item["id"]] = fetch(item, allowed, args.out)
        if is_removal(item):
            index[item["id"]].update(fetch_removed(item, allowed, args.out))
        print(f"{item['id']}: {index[item['id']].get('file') or 'missing, ' + index[item['id']]['missing']}")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
