#!/usr/bin/env python3
"""Write the review inputs for one claim pull request. Reads the pull request as data; runs nothing from it.

Usage:
    python rules/scripts/review_context.py --pr N --head SHA --pr-dir pr --out context

Writes context/findings.json (from the pull request body's findings block),
context/diff.patch (data/tools/ only) and context/pr.json. Stops with an error
if the head moved, or if the diff and the findings disagree (check_diff.py's
claim check), so no review is posted and automerge.yml refuses the pull request.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import findings as block  # noqa: E402
import ghapi  # noqa: E402
from check_diff import changed_paths, check_claim, git, merge_base, reader  # noqa: E402


def build(pull, pr_dir, head, out):
    """Write the three context files. Returns the problems that stop the review (empty if none)."""
    if pull["head"]["sha"] != head:
        return [f"head moved from {head[:7]} to {pull['head']['sha'][:7]}"]
    try:
        items = block.parse(pull["body"])
    except ValueError as exc:
        return [str(exc)]
    base = merge_base(pr_dir, pull["base"]["sha"], head)
    problems = check_claim(reader(pr_dir, base), reader(pr_dir, head), changed_paths(pr_dir, base, head), items)
    if problems:
        return problems
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "findings.json").write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "diff.patch").write_text(git(pr_dir, "diff", f"{base}...{head}", "--", "data/tools/"), encoding="utf-8")
    meta = {"number": pull["number"], "head": head, "base": base, "title": pull["title"]}
    (out / "pr.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--pr-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    pull = ghapi.get(f"repos/{ghapi.repo()}/pulls/{args.pr}")
    problems = build(pull, args.pr_dir, args.head, args.out)
    for problem in problems:
        print(f"stopped: {problem}")
    if problems:
        return 1
    print(f"context written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
