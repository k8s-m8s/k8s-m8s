#!/usr/bin/env python3
"""Mechanical checks on an automated pull request's diff. Reads files with git; runs nothing from them.

Usage:
    python scripts/check_diff.py bookkeeping BASE HEAD
    python scripts/check_diff.py claim BASE HEAD --findings findings.json
    python scripts/check_diff.py claim BASE WORKTREE --findings ...   # compare with the working tree
    python scripts/check_diff.py window BASE HEAD --findings findings.json

BASE...HEAD is compared from their merge base, as a pull request shows it.
Exit status 0 means the diff passes; otherwise every problem is printed.

bookkeeping: only `verified` and `source_commit` differ, on existing entries
    of data/tools/*.yaml and in data/kubernetes.yaml; nothing else changed.
claim: exactly one data/tools/<tool>.yaml changed and nothing else; its header
    is unchanged; no entry was removed; at most MAX_ENTRIES entries changed;
    each has an allowlisted source and a full source_commit; the changed
    entries are exactly the findings, with the findings' old and new values.
    An entry may become delisted (a removal) only if its finding names the
    upstream commit where the row is gone, at most MAX_REMOVALS times, and
    only while a current entry remains. Entries that are already delisted
    never change, and no entry is added already delisted.
window: only data/kubernetes.yaml changed, and only its window, source_commit
    and verified; the new window is the old one rolled forward by exactly one
    minor; the one kubernetes/window finding has the diff's old and new values.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import _Loader, dump_releases, ordered, release_label  # noqa: E402
from validate import is_under  # noqa: E402
from verify import snapshot  # noqa: E402

MAX_ENTRIES = 10
MAX_REMOVALS = 2
BOOKKEEPING_KEYS = {"verified", "source_commit"}
WINDOW_KEYS = {"window", "source_commit", "verified"}
KUBERNETES_FILE = "data/kubernetes.yaml"
TOOL_FILE = re.compile(r"data/tools/([a-z0-9][a-z0-9-]*)\.yaml")
SHA = re.compile(r"[0-9a-f]{40}")
MINOR = re.compile(r"1\.(\d+)")
WORKTREE = "WORKTREE"


def git(repo_dir, *args):
    return subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True, text=True).stdout


def reader(repo_dir, ref):
    """A function returning a file's text at ref (or in the working tree), or None if it does not exist."""
    def read(path):
        if ref == WORKTREE:
            file = Path(repo_dir) / path
            return file.read_text(encoding="utf-8") if file.is_file() else None
        result = subprocess.run(["git", "-C", str(repo_dir), "show", f"{ref}:{path}"], capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else None
    return read


def changed_paths(repo_dir, base, head):
    if head == WORKTREE:
        tracked = git(repo_dir, "diff", "--name-only", base, "--").split()
        untracked = git(repo_dir, "ls-files", "--others", "--exclude-standard").split()
        return sorted(set(tracked + untracked))
    return sorted(git(repo_dir, "diff", "--name-only", f"{base}...{head}", "--").split())


def merge_base(repo_dir, base, head):
    if head == WORKTREE:
        return git(repo_dir, "rev-parse", base).strip()
    return git(repo_dir, "merge-base", base, head).strip()


def parse(text):
    return yaml.load(text, Loader=_Loader)


def split_tool(text):
    head, marker, body = text.partition("\nreleases:\n")
    return head, marker, body


def canonical_problem(path, text):
    """The releases block must be exactly what the writer produces, so no comments or extra keys hide in it."""
    head, marker, body = split_tool(text)
    if not marker:
        return f"{path}: no 'releases:' line"
    try:
        releases = parse(text)["releases"]
        if dump_releases(ordered(releases)) != body:
            return f"{path}: releases are not in the layout scripts/common.py writes"
    except Exception as exc:  # malformed YAML or missing fields
        return f"{path}: cannot read releases ({exc})"
    return None


def without(entry, keys):
    return {k: v for k, v in entry.items() if k not in keys}


def check_bookkeeping(read_base, read_head, paths):
    problems = []
    if not paths:
        problems.append("no files changed")
    for path in paths:
        before, after = read_base(path), read_head(path)
        if before is None or after is None:
            problems.append(f"{path}: added or removed")
            continue
        if path == "data/kubernetes.yaml":
            if without(parse(before), BOOKKEEPING_KEYS) != without(parse(after), BOOKKEEPING_KEYS):
                problems.append(f"{path}: changes other than verified or source_commit")
            strip = lambda t: [l for l in t.splitlines() if not re.match(r"(verified|source_commit):", l)]  # noqa: E731
            if strip(before) != strip(after):
                problems.append(f"{path}: text changed outside verified and source_commit")
            continue
        if not TOOL_FILE.fullmatch(path):
            problems.append(f"{path}: outside data/tools/ and data/kubernetes.yaml")
            continue
        problem = canonical_problem(path, after)
        if problem:
            problems.append(problem)
            continue
        if split_tool(before)[0] != split_tool(after)[0]:
            problems.append(f"{path}: file header changed")
        old, new = parse(before)["releases"], parse(after)["releases"]
        if [release_label(r) for r in old] != [release_label(r) for r in new]:
            problems.append(f"{path}: entries added, removed or reordered")
            continue
        for a, b in zip(old, new):
            if without(a, BOOKKEEPING_KEYS) != without(b, BOOKKEEPING_KEYS):
                problems.append(f"{path}: {release_label(a)} changed beyond verified and source_commit")
            elif "source_commit" in b and not SHA.fullmatch(b["source_commit"]):
                problems.append(f"{path}: {release_label(a)} source_commit is not a full commit")
    return problems


def removal_problems(label, prior, entry, item):
    """Why a newly delisted entry can't merge automatically (empty if it can).

    A plain removal only adds `delisted`. A row read from upstream's history may
    also take the values, source and commit of the last commit that listed it.
    Either way the finding must name the upstream commit where the row is gone,
    so the review can confirm the removal.
    """
    if prior is None:
        return [f"{label}: added already delisted; recovering history needs a human"]
    problems = []
    if not SHA.fullmatch(str(item.get("removed_commit") or "")):
        problems.append(f"{label}: no upstream commit showing the removal")
    if item.get("fields") == ["delisted"] and without(entry, {"delisted"}) != prior:
        problems.append(f"{label}: changed beyond delisted")
    return problems


def check_claim(read_base, read_head, paths, findings, max_entries=MAX_ENTRIES):
    problems = []
    tools = {TOOL_FILE.fullmatch(p).group(1) for p in paths if TOOL_FILE.fullmatch(p)}
    others = [p for p in paths if not TOOL_FILE.fullmatch(p)]
    if others:
        problems.append("files outside data/tools/ changed: " + ", ".join(others))
    if len(tools) != 1:
        return problems + [f"expected changes to exactly one tool, found {len(tools)}"]
    tool = tools.pop()
    path = f"data/tools/{tool}.yaml"
    before, after = read_base(path), read_head(path)
    if before is None or after is None:
        return problems + [f"{path}: added or removed; a new tool needs a human"]
    problem = canonical_problem(path, after)
    if problem:
        return problems + [problem]
    if split_tool(before)[0] != split_tool(after)[0]:
        problems.append(f"{path}: file header changed (name, repo, official_sources or notes)")

    base_doc, head_doc = parse(before), parse(after)
    allowed = [base_doc["repo"], *base_doc.get("official_sources", [])]  # from the base, never the pull request
    old = {release_label(r): r for r in base_doc["releases"]}
    new = {release_label(r): r for r in head_doc["releases"]}
    removed = sorted(set(old) - set(new))
    if removed:
        problems.append("entries removed: " + ", ".join(removed))
    claim_findings = [f for f in findings if f.get("class") == "claim"]
    by_id = {f["id"]: f for f in claim_findings}
    changed, removals = [], []
    for label, entry in new.items():
        prior = old.get(label)
        if prior == entry:
            continue
        changed.append(label)
        if prior and "delisted" in prior:
            problems.append(f"{label}: changes an entry that is already delisted")
        elif "delisted" in entry:
            removals.append(label)
            problems += removal_problems(label, prior, entry, by_id.get(f"{tool}/{label}") or {})
        if not any(is_under(entry["source"], prefix) for prefix in allowed):
            problems.append(f"{label}: source {entry['source']} is not under the tool's repo or official_sources")
        if not SHA.fullmatch(str(entry.get("source_commit", ""))):
            problems.append(f"{label}: no full source_commit")
    if len(changed) > max_entries:
        problems.append(f"{len(changed)} entries changed; the limit is {max_entries}")
    if len(removals) > MAX_REMOVALS:
        problems.append(f"{len(removals)} entries delisted; the limit is {MAX_REMOVALS}")
    if removals and all("delisted" in r for r in head_doc["releases"]):
        problems.append("every entry would be delisted")

    if len(claim_findings) != len(findings):
        problems.append("findings include classes other than claim")
    if any(f.get("tool") != tool for f in findings):
        problems.append(f"findings name a tool other than {tool}")
    expected = {f"{tool}/{label}" for label in changed}
    if set(by_id) != expected or len(by_id) != len(claim_findings):
        problems.append(f"changed entries {sorted(expected)} do not match findings {sorted(by_id)}")
    else:
        for label in changed:
            item = by_id[f"{tool}/{label}"]
            if item.get("old") != snapshot(old.get(label)) or item.get("new") != snapshot(new[label]):
                problems.append(f"{label}: diff does not match the finding's old and new values")
    return problems


def rolled(window):
    """The window after the next Kubernetes minor is released, or None if window isn't three consecutive minors."""
    if not (isinstance(window, list) and len(window) == 3 and all(isinstance(m, str) and MINOR.fullmatch(m) for m in window)):
        return None
    numbers = [int(MINOR.fullmatch(m).group(1)) for m in window]
    if numbers != [numbers[0], numbers[0] - 1, numbers[0] - 2]:
        return None
    return [f"1.{numbers[0] + 1}", window[0], window[1]]


def check_window(read_base, read_head, paths, findings):
    if paths != [KUBERNETES_FILE]:
        return [f"expected only {KUBERNETES_FILE} to change, found: {', '.join(paths) or 'nothing'}"]
    before, after = read_base(KUBERNETES_FILE), read_head(KUBERNETES_FILE)
    if before is None or after is None:
        return [f"{KUBERNETES_FILE}: added or removed"]
    problems = []
    strip = lambda t: [l for l in t.splitlines() if not re.match(r"(window|source_commit|verified):", l)]  # noqa: E731
    if strip(before) != strip(after):
        problems.append(f"{KUBERNETES_FILE}: text changed outside window, source_commit and verified")
    try:
        old, new = parse(before), parse(after)
    except yaml.YAMLError as exc:
        return problems + [f"{KUBERNETES_FILE}: cannot read ({exc})"]
    if without(old, WINDOW_KEYS) != without(new, WINDOW_KEYS):
        problems.append(f"{KUBERNETES_FILE}: keys other than window, source_commit and verified changed")
    expected = rolled(old.get("window"))
    if expected is None or new.get("window") != expected:
        problems.append(f"window {new.get('window')} is not {old.get('window')} rolled forward by one minor")
    if not SHA.fullmatch(str(new.get("source_commit", ""))):
        problems.append(f"{KUBERNETES_FILE}: source_commit is not a full commit")
    if [(f.get("id"), f.get("class")) for f in findings] != [("kubernetes/window", "window")]:
        problems.append("findings are not the one kubernetes/window finding")
    elif (findings[0].get("old") != {"window": old.get("window"), "source_commit": old.get("source_commit")}
          or findings[0].get("new") != {"window": new.get("window"), "source_commit": new.get("source_commit")}):
        problems.append("diff does not match the finding's old and new values")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=["bookkeeping", "claim", "window"])
    parser.add_argument("base")
    parser.add_argument("head", help=f"a ref, or {WORKTREE} for the working tree")
    parser.add_argument("--findings", help="JSON file with the pull request's findings (claim and window)")
    parser.add_argument("--repo-dir", default=".", help="git checkout to read from (default: current directory)")
    args = parser.parse_args()

    base = merge_base(args.repo_dir, args.base, args.head)
    paths = changed_paths(args.repo_dir, base, args.head)
    read_base, read_head = reader(args.repo_dir, base), reader(args.repo_dir, args.head)
    if args.kind == "bookkeeping":
        problems = check_bookkeeping(read_base, read_head, paths)
    else:
        if not args.findings:
            parser.error(f"{args.kind} needs --findings")
        with open(args.findings, encoding="utf-8") as handle:
            check = check_claim if args.kind == "claim" else check_window
            problems = check(read_base, read_head, paths, json.load(handle))
    for problem in problems:
        print(f"refused: {problem}")
    if not problems:
        print(f"OK: {args.kind} diff passes ({len(paths)} file(s))")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
