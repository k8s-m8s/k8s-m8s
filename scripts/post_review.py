#!/usr/bin/env python3
"""Validate Claude's verdict, render the review comment, and post it as the reviewer App.

Usage:
    python rules/scripts/post_review.py render --context context --verdict out/verdict.json \
        --execution-file "$EXECUTION_FILE" --run-url URL --out comment.md
    python rules/scripts/post_review.py post --context context --body comment.md   # GH_TOKEN: reviewer App

render needs no token. It accepts the verdict only if:
  - it matches schema/review.schema.json, with exactly one row per finding id;
  - no reason looks like a credential;
  - Claude's session log shows only Read, Glob, Grep and the structured output
    tool, reading only inside the workspace.
Otherwise the comment is `rejected` and says why. post adds nothing to the
body; the head SHA in the marker comes from context/pr.json, never from Claude.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ghapi  # noqa: E402
import reviews  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "review.schema.json"
ALLOWED_TOOLS = {"Read", "Glob", "Grep", "StructuredOutput"}
READABLE = ("context/", "upstream/", "claude-review.md", "rules/")
CREDENTIAL = re.compile(r"sk-ant-|gh[pousr]_[A-Za-z0-9]|github_pat_|-----BEGIN")
LONG_TOKEN = re.compile(r"[A-Za-z0-9_-]{40,}")
COMMIT = re.compile(r"[0-9a-f]{40}")


def looks_like_credential(text):
    """Known token prefixes, or a long unbroken token that is not a commit SHA."""
    return bool(CREDENTIAL.search(text)) or any(not COMMIT.fullmatch(t) for t in LONG_TOKEN.findall(text))


def check_verdict(verdict, ids):
    """Problems with Claude's verdict (empty if it is acceptable)."""
    if verdict is None:
        return ["no verdict was returned"]
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(verdict), key=lambda e: list(e.absolute_path))
    if errors:
        return [f"verdict does not match the schema at {'/'.join(map(str, e.absolute_path)) or '(root)'}" for e in errors[:5]]
    got = [row["id"] for row in verdict["rows"]]
    problems = []
    if len(got) != len(set(got)):
        problems.append("a finding has more than one row")
    if set(got) - set(ids):
        problems.append(f"{len(set(got) - set(ids))} row(s) for findings that do not exist")
    if set(ids) - set(got):
        problems.append(f"{len(set(ids) - set(got))} finding(s) without a row")
    if any(re.search(r"<!--|-->|k8s-m8s-(review|findings|automerge)", row["reason"]) for row in verdict["rows"]):
        problems.append("a reason contains a comment marker; it is shown escaped")
    if any(looks_like_credential(row["reason"]) for row in verdict["rows"]):
        problems.append("a reason contains text that looks like a credential, so no reasons are shown")
    return problems


def tool_calls(log):
    """Every tool_use block anywhere in the session log."""
    if isinstance(log, dict):
        if log.get("type") == "tool_use":
            yield log
        for value in log.values():
            yield from tool_calls(value)
    elif isinstance(log, list):
        for value in log:
            yield from tool_calls(value)


def inside(path, workspace):
    """True if a tool call's path stays within the review inputs in the workspace."""
    if path is None or path == "":
        return True
    if not isinstance(path, str):
        return False
    resolved = (Path(workspace) / path).resolve()  # an absolute path replaces the workspace
    try:
        relative = resolved.relative_to(Path(workspace).resolve()).as_posix()
    except ValueError:
        return False
    return relative == "." or any(relative == r.rstrip("/") or relative.startswith(r) for r in READABLE)


def check_session(execution_file, workspace):
    if not execution_file or not Path(execution_file).is_file():
        return ["Claude's session log is missing, so the tools it used cannot be checked"]
    try:
        log = json.loads(Path(execution_file).read_text(encoding="utf-8"))
    except ValueError:
        return ["Claude's session log is not JSON"]
    problems = set()
    for call in tool_calls(log):
        name, args = call.get("name"), call.get("input") or {}
        if name not in ALLOWED_TOOLS:
            problems.add(f"Claude used a tool that is not allowed: {name}")
        elif not all(inside(args.get(key), workspace) for key in ("file_path", "path")):
            problems.add(f"Claude's {name} call reached outside the review inputs")
    return sorted(problems)


def render(args):
    context = Path(args.context)
    pr = json.loads((context / "pr.json").read_text(encoding="utf-8"))
    ids = [item["id"] for item in json.loads((context / "findings.json").read_text(encoding="utf-8"))]
    try:
        verdict = json.loads(Path(args.verdict).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        verdict = None
    problems = check_session(args.execution_file, args.workspace) + check_verdict(verdict, ids)
    hide_reasons = any("credential" in p for p in problems)
    rows = [] if verdict is None or not isinstance(verdict, dict) or not isinstance(verdict.get("rows"), list) else [
        {"id": str(r.get("id")), "verdict": str(r.get("verdict")),
         "reason": "(hidden)" if hide_reasons else str(r.get("reason", ""))[:300]}
        for r in verdict["rows"] if isinstance(r, dict)
    ]
    body = reviews.render(pr["head"], rows, problems, args.run_url)
    Path(args.out).write_text(body, encoding="utf-8")
    print(body)
    return 0


def post(args):
    pr = json.loads((Path(args.context) / "pr.json").read_text(encoding="utf-8"))
    body = Path(args.body).read_text(encoding="utf-8")
    reviews.parse(body)  # refuse to post anything that is not exactly one review
    comment = ghapi.call("POST", f"repos/{ghapi.repo()}/issues/{pr['number']}/comments", {"body": body})
    print(f"posted {comment['html_url']}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["render", "post"])
    parser.add_argument("--context", required=True)
    parser.add_argument("--verdict")
    parser.add_argument("--execution-file")
    parser.add_argument("--workspace", default=os.environ.get("GITHUB_WORKSPACE", "."))
    parser.add_argument("--run-url", default="")
    parser.add_argument("--out")
    parser.add_argument("--body")
    args = parser.parse_args()
    if args.command == "render":
        if not (args.verdict and args.out):
            parser.error("render needs --verdict and --out")
        return render(args)
    if not args.body:
        parser.error("post needs --body")
    return post(args)


if __name__ == "__main__":
    sys.exit(main())
