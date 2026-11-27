#!/usr/bin/env python3
"""Decide which automated pull requests may merge, then act on the decision.

Usage (automerge.yml, run from main):
    python scripts/automerge.py decide --out actions.json [--pr N]   # GH_TOKEN: read-only workflow token
    python scripts/automerge.py act --actions actions.json            # GH_TOKEN: automation App token

decide reads pull requests, comments, checks and workflow runs, reads the
diff with `git show` from the main checkout (nothing from the pull request is
run), and writes the actions to take. act performs them: squash merges (pinned
to the checked head SHA), labels and comments. With AUTOMERGE_DRY_RUN=true,
claim and window pull requests get a "would merge" or "would not merge"
comment instead of a merge; bookkeeping pull requests merge either way.
Nothing labelled human-merge, and no test fixture, is ever merged.

Environment: AUTOMERGE_ENABLED, AUTOMERGE_DRY_RUN, AUTOMATION_LOGIN,
REVIEWER_LOGIN, GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import findings as block  # noqa: E402
import ghapi  # noqa: E402
import reviews  # noqa: E402
from check_diff import (KUBERNETES_FILE, MINOR, changed_paths, check_bookkeeping, check_claim,  # noqa: E402
                        check_window, merge_base, parse, reader)

ROOT = Path(__file__).resolve().parent.parent
MIN_AGE = datetime.timedelta(hours=2)
DECISION = re.compile(r"<!-- k8s-m8s-automerge head=([0-9a-f]{40}) decision=([a-z-]+) -->")
PASSED = {"success", "neutral", "skipped"}
WINDOW_BRANCH = "automated/window"
FIXTURE_TITLE = "[test fixture]"


class Wait(Exception):
    """A gate is not decided yet (checks running, no review for this head, too young)."""


class Refuse(Exception):
    """A gate failed in a way that needs a maintainer: label human-merge."""


def when(stamp):
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def validate_state(sha):
    runs = ghapi.get(f"repos/{ghapi.repo()}/actions/workflows/validate.yml/runs", head_sha=sha)["workflow_runs"]
    if not runs:
        return "missing"
    latest = max(runs, key=lambda r: (r["created_at"], r["id"]))
    return "success" if latest["conclusion"] == "success" else latest["status"] if latest["status"] != "completed" else "failure"


def checks_state(sha):
    """(running, failed): the names of the checks on a commit that have not finished, and that did not pass.

    Check runs and commit statuses both count. Only the newest run of each check
    (by app and name) is read, so a re-run that passes replaces a failure.
    """
    repo = ghapi.repo()
    newest = {}
    for run in ghapi.paged(f"repos/{repo}/commits/{sha}/check-runs", key="check_runs", filter="all"):
        key = ((run.get("app") or {}).get("id"), run["name"])
        if key not in newest or run["id"] > newest[key]["id"]:
            newest[key] = run
    running = [r["name"] for r in newest.values() if r["status"] != "completed"]
    failed = [r["name"] for r in newest.values() if r["status"] == "completed" and r["conclusion"] not in PASSED]
    for status in ghapi.get(f"repos/{repo}/commits/{sha}/status").get("statuses", []):
        if status["state"] == "pending":
            running.append(status["context"])
        elif status["state"] != "success":
            failed.append(status["context"])
    return sorted(running), sorted(failed)


def fetch_head(pull):
    """Make the pull request's head commit readable with git show in the main checkout."""
    subprocess.run(["git", "-C", str(ROOT), "fetch", "--quiet", "--no-tags", "origin",
                    f"+refs/pull/{pull['number']}/head:refs/remotes/pull/{pull['number']}"], check=True)
    fetched = subprocess.run(["git", "-C", str(ROOT), "rev-parse", f"refs/remotes/pull/{pull['number']}"],
                             check=True, capture_output=True, text=True).stdout.strip()
    if fetched != pull["head"]["sha"]:
        raise Wait("head moved while checking")
    base = merge_base(ROOT, pull["base"]["sha"], fetched)
    return base, fetched


def author_gates(pull, automation):
    if (pull["head"].get("repo") or {}).get("full_name") != ghapi.repo():
        raise Refuse("head is not in this repository")
    if pull["user"]["login"] != automation or pull["user"].get("type") != "Bot":
        raise Refuse(f"author is {pull['user']['login']}, not {automation}")


def checks_gate(pull):
    """Every check on the head passed: validate.yml, which must have run, and any other check or status."""
    sha = pull["head"]["sha"]
    state = validate_state(sha)
    running, failed = checks_state(sha)
    if state == "failure":
        failed = sorted({*failed, "validate"})
    if failed:
        raise Refuse("checks failed on the head commit: " + ", ".join(failed))
    if state != "success":
        raise Wait(f"validate.yml is {state} on the head commit")
    if running:
        raise Wait("checks are still running on the head commit: " + ", ".join(running))


def labelled(pull):
    return any(label["name"] == "human-merge" for label in pull["labels"])


def findings_of(pull):
    try:
        return block.parse(pull["body"])
    except ValueError as exc:
        raise Refuse(f"findings block: {exc}") from None


def bookkeeping_gates(pull, automation):
    author_gates(pull, automation)
    checks_gate(pull)
    base, head = fetch_head(pull)
    problems = check_bookkeeping(reader(ROOT, base), reader(ROOT, head), changed_paths(ROOT, base, head))
    if problems:
        raise Refuse("bookkeeping diff check failed: " + "; ".join(problems))


def claim_gates(pull, automation, reviewer, now):
    """Gates 1 to 9 in docs/maintainers/automation.md. Returns what the merge comment links to."""
    if not pull["head"]["ref"].startswith("automated/claim/"):
        raise Refuse("head branch is not automated/claim/*")
    author_gates(pull, automation)                                   # gate 1
    if labelled(pull):                                               # gate 2
        raise Wait("labelled human-merge")
    checks_gate(pull)                                                # gate 3
    items = findings_of(pull)
    base, head = fetch_head(pull)
    problems = check_claim(reader(ROOT, base), reader(ROOT, head), changed_paths(ROOT, base, head), items)
    if problems:                                                     # gate 4
        raise Refuse("claim diff check failed: " + "; ".join(problems))

    comments = ghapi.paged(f"repos/{ghapi.repo()}/issues/{pull['number']}/comments")
    review = reviews.latest(comments, reviewer)                      # gate 5
    if review is None:
        raise Wait("no review yet")
    if review["created_at"] != review["updated_at"]:
        raise Refuse("the newest review comment was edited")
    try:
        reviewed, verdict, rows = reviews.parse(review["body"])
    except ValueError as exc:
        raise Refuse(f"the newest review comment is not a valid review: {exc}") from None
    if reviewed != head:
        raise Wait(f"newest review is for {reviewed[:7]}, not the head {head[:7]}")
    ids = {item["id"] for item in items}
    if verdict != "confirmed":
        raise Refuse("review verdict is rejected")
    if set(rows) != ids or any(v != "confirmed" for v in rows.values()):  # gate 6
        raise Refuse("review rows do not confirm every finding")
    committed = ghapi.get(f"repos/{ghapi.repo()}/commits/{head}")["commit"]["committer"]["date"]
    if when(committed) > when(review["created_at"]):                 # gate 7
        raise Refuse("commits were pushed after the review")
    if now - when(pull["created_at"]) < MIN_AGE:                     # gate 8
        raise Wait("open for less than 2 hours")
    if pull["title"].startswith(FIXTURE_TITLE):                      # gate 9, last so a rehearsal shows the review's verdict
        raise Refuse("a test fixture is never merged")
    return f"[Review]({review['html_url']})"


def kubernetes_release(minor):
    """Kubernetes' own release record for a minor's first release. Waits until it is published as a full release."""
    tag = f"v{minor}.0"
    try:
        release = ghapi.get(f"repos/kubernetes/kubernetes/releases/tags/{ghapi.quote(tag)}")
    except ghapi.GitHubError as err:
        if err.status == 404:
            raise Wait(f"Kubernetes {tag} is not published yet") from None
        raise
    if release.get("draft") or release.get("prerelease"):
        raise Wait(f"Kubernetes {tag} is not a full release yet")
    return f"Kubernetes [{tag}]({release['html_url']}) is released"


def window_gates(pull, automation, now):
    """The window gates in docs/maintainers/automation.md. Returns what the merge comment says."""
    if pull["head"]["ref"] != WINDOW_BRANCH:
        raise Refuse(f"head branch is not {WINDOW_BRANCH}")
    author_gates(pull, automation)
    if labelled(pull):
        raise Wait("labelled human-merge")
    checks_gate(pull)
    items = findings_of(pull)
    base, head = fetch_head(pull)
    read_head = reader(ROOT, head)
    problems = check_window(reader(ROOT, base), read_head, changed_paths(ROOT, base, head), items)
    if problems:
        raise Refuse("window diff check failed: " + "; ".join(problems))
    newest = parse(read_head(KUBERNETES_FILE))["window"][0]
    if not MINOR.fullmatch(newest):  # check_window already requires this; the release lookup relies on it
        raise Refuse(f"unexpected minor {newest!r}")
    released = kubernetes_release(newest)
    if now - when(pull["created_at"]) < MIN_AGE:
        raise Wait("open for less than 2 hours")
    return released


def last_decision(comments, automation, head):
    for c in sorted(comments, key=lambda c: (c["created_at"], c["id"]), reverse=True):
        if c["user"]["login"] == automation:
            match = DECISION.search(c["body"] or "")
            if match:
                return match.groups() if match.group(1) == head else None
    return None


def decide(pull, env, now):
    """Actions for one pull request: a list of dicts that act() performs."""
    automation, reviewer = env["AUTOMATION_LOGIN"], env["REVIEWER_LOGIN"]
    dry_run = env.get("AUTOMERGE_DRY_RUN", "true") != "false"
    number, head, ref = pull["number"], pull["head"]["sha"], pull["head"]["ref"]
    if ref == "automated/bookkeeping":
        try:
            bookkeeping_gates(pull, automation)
        except Wait as exc:
            print(f"#{number}: waiting, {exc}")
            return []
        except Refuse as exc:
            print(f"#{number}: refused, {exc}")
            return [] if labelled(pull) else [
                {"pr": number, "action": "label", "label": "human-merge", "comment": f"Not merging automatically: {exc}."}]
        return [{"pr": number, "action": "merge", "sha": head,
                 "comment": "Merged automatically: only `verified` and `source_commit` changed, and every check passed."}]
    if ref == WINDOW_BRANCH:
        gates = lambda: window_gates(pull, automation, now)  # noqa: E731
    elif ref.startswith("automated/claim/"):
        gates = lambda: claim_gates(pull, automation, reviewer, now)  # noqa: E731
    else:
        print(f"#{number}: {ref} never merges automatically")
        return []
    try:
        detail = gates()
    except Wait as exc:
        print(f"#{number}: waiting, {exc}")
        return []
    except Refuse as exc:
        print(f"#{number}: refused, {exc}")
        actions = [] if labelled(pull) else [{"pr": number, "action": "label", "label": "human-merge"}]
        comments = ghapi.paged(f"repos/{ghapi.repo()}/issues/{number}/comments")
        if last_decision(comments, automation, head) != (head, "refused"):
            prefix = "Would not merge" if dry_run else "Not merging"
            actions.append({"pr": number, "action": "comment",
                            "comment": f"{prefix}, reason: {exc}. Labelled `human-merge`.\n\n"
                                       f"<!-- k8s-m8s-automerge head={head} decision=refused -->"})
        return actions
    if dry_run:
        comments = ghapi.paged(f"repos/{ghapi.repo()}/issues/{number}/comments")
        if last_decision(comments, automation, head) == (head, "would-merge"):
            return []
        return [{"pr": number, "action": "comment",
                 "comment": f"Would merge (dry run): every gate passed. {detail}.\n\n"
                            f"<!-- k8s-m8s-automerge head={head} decision=would-merge -->"}]
    return [{"pr": number, "action": "merge", "sha": head,
             "comment": f"Merged automatically: every gate passed. {detail}."}]


def act(actions):
    repo = ghapi.repo()
    for item in actions:
        number = item["pr"]
        if item["action"] == "merge":
            try:
                ghapi.call("PUT", f"repos/{repo}/pulls/{number}/merge", {"merge_method": "squash", "sha": item["sha"]})
            except ghapi.GitHubError as err:
                print(f"#{number}: merge failed, {err}")
                continue
            ghapi.call("POST", f"repos/{repo}/issues/{number}/comments", {"body": item["comment"]})
            pull = ghapi.get(f"repos/{repo}/pulls/{number}")
            try:
                ghapi.call("DELETE", f"repos/{repo}/git/refs/heads/{pull['head']['ref']}")
            except ghapi.GitHubError:
                pass
            print(f"#{number}: merged")
        elif item["action"] == "label":
            ghapi.call("POST", f"repos/{repo}/issues/{number}/labels", {"labels": [item["label"]]})
            if item.get("comment"):
                ghapi.call("POST", f"repos/{repo}/issues/{number}/comments", {"body": item["comment"]})
            print(f"#{number}: labelled {item['label']}")
        elif item["action"] == "comment":
            ghapi.call("POST", f"repos/{repo}/issues/{number}/comments", {"body": item["comment"]})
            print(f"#{number}: commented")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["decide", "act"])
    parser.add_argument("--out")
    parser.add_argument("--actions")
    parser.add_argument("--pr", type=int, help="only this pull request (default: every open automated/* one)")
    args = parser.parse_args()
    env = os.environ
    if env.get("AUTOMERGE_ENABLED") != "true":
        print("AUTOMERGE_ENABLED is not true; doing nothing")
        if args.out:
            Path(args.out).write_text("[]\n", encoding="utf-8")
        return 0
    if args.command == "act":
        act(json.loads(Path(args.actions).read_text(encoding="utf-8")))
        return 0
    repo = ghapi.repo()
    pulls = [ghapi.get(f"repos/{repo}/pulls/{args.pr}")] if args.pr else \
        [ghapi.get(f"repos/{repo}/pulls/{p['number']}") for p in ghapi.paged(f"repos/{repo}/pulls", state="open")
         if p["head"]["ref"].startswith("automated/")]
    now = datetime.datetime.now(datetime.timezone.utc)
    actions = []
    for pull in pulls:
        if pull["state"] == "open":
            actions += decide(pull, env, now)
    Path(args.out).write_text(json.dumps(actions, indent=2) + "\n", encoding="utf-8")
    print(f"{len(actions)} action(s)")
    if "GITHUB_OUTPUT" in env:
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write(f"act={'true' if actions else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
