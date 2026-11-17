#!/usr/bin/env python3
"""Choose the claim pull requests review.yml should review, and print them as a JSON list.

Usage (in review.yml's select job, with a read-only GH_TOKEN):
    python scripts/review_select.py >> "$GITHUB_OUTPUT"      # writes prs=[{"pr": N, "head": SHA}, ...]

workflow_run: the pull request whose validate.yml run just succeeded, if its
    head is still that run's commit.
workflow_dispatch: the pull request in INPUT_PR, reviewed again even if it
    already has a review for its head.
schedule: every open claim pull request with no review for its current head.

A pull request qualifies only if it is open, its head branch is
automated/claim/* in this repository, its author is AUTOMATION_LOGIN, it has
no human-merge label, and validate.yml passed on its head commit.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ghapi  # noqa: E402
import reviews  # noqa: E402

PREFIX = "automated/claim/"


def validate_passed(sha):
    runs = ghapi.get(f"repos/{ghapi.repo()}/actions/workflows/validate.yml/runs", head_sha=sha)["workflow_runs"]
    if not runs:
        return False
    latest = max(runs, key=lambda r: (r["created_at"], r["id"]))
    return latest["status"] == "completed" and latest["conclusion"] == "success"


def qualifies(pull, automation):
    """Why a pull request is out of scope for review, or None if it is in scope."""
    if pull["state"] != "open":
        return "not open"
    if (pull["head"].get("repo") or {}).get("full_name") != ghapi.repo():
        return "head is not in this repository"
    if not pull["head"]["ref"].startswith(PREFIX):
        return "head branch is not automated/claim/*"
    if pull["user"]["login"] != automation:
        return f"author is {pull['user']['login']}"
    if any(label["name"] == "human-merge" for label in pull["labels"]):
        return "labelled human-merge"
    return None


def candidates(event_name, event):
    repo = ghapi.repo()
    if event_name == "workflow_run":
        run = event["workflow_run"]
        if run.get("conclusion") != "success" or (run.get("head_repository") or {}).get("full_name") != repo:
            return []
        if not run.get("head_branch", "").startswith(PREFIX):
            return []
        numbers = [p["number"] for p in run.get("pull_requests") or []]
        if not numbers:
            owner = repo.split("/")[0]
            numbers = [p["number"] for p in ghapi.get(f"repos/{repo}/pulls", state="open", head=f"{owner}:{run['head_branch']}")]
        pulls = [ghapi.get(f"repos/{repo}/pulls/{n}") for n in numbers]
        return [p for p in pulls if p["head"]["sha"] == run["head_sha"]]  # re-checked against the API
    if event_name == "workflow_dispatch":
        number = str(os.environ.get("INPUT_PR", "")).strip()
        if not number.isdigit():
            raise SystemExit("workflow_dispatch needs a pull request number")
        return [ghapi.get(f"repos/{repo}/pulls/{number}")]
    return [p for p in ghapi.paged(f"repos/{repo}/pulls", state="open") if p["head"]["ref"].startswith(PREFIX)]


def main():
    event_name = os.environ["GITHUB_EVENT_NAME"]
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        event = json.load(handle)
    automation, reviewer = os.environ["AUTOMATION_LOGIN"], os.environ["REVIEWER_LOGIN"]
    selected = []
    for pull in candidates(event_name, event):
        number, head = pull["number"], pull["head"]["sha"]
        reason = qualifies(pull, automation)
        if not reason and not validate_passed(head):
            reason = "validate.yml has not passed on the head commit"
        if not reason and event_name != "workflow_dispatch":
            comments = ghapi.paged(f"repos/{ghapi.repo()}/issues/{number}/comments")
            if head in reviews.reviewed_heads(comments, reviewer):
                reason = "already reviewed at this head"
        print(f"#{number} {head[:7]}: {reason or 'selected'}", file=sys.stderr)
        if not reason:
            selected.append({"pr": number, "head": head})
    print("prs=" + json.dumps(selected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
