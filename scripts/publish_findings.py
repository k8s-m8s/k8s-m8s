#!/usr/bin/env python3
"""Publish a verify.py report as pull requests and issues. Run only from main, in the publish job.

Usage:
    python scripts/publish_findings.py detect --report R --data-dir D --base SHA
    python scripts/publish_findings.py bookkeeping --report R --data-dir D --base SHA
    python scripts/publish_findings.py pending --report R      # prints "true" if publishing is needed

`D` holds the data files the read-only job wrote (D/data/...), and `SHA` is the
main commit they were computed from. Commits are made through the GitHub API
on top of SHA, so no git credentials are stored. GH_TOKEN is the automation
App's token, minted for this step only.

detect:
  - claim findings: one pull request per tool from automated/claim/<tool>,
    labelled claim-change, and human-merge if check_diff.py's claim check
    (merge gate 4) would refuse it. The branch is only rewritten when the
    findings change, or when the pull request can no longer be merged, so a
    review stays valid between runs. A claim pull request whose tool now
    matches upstream is closed.
  - window findings: a pull request from automated/window, labelled
    window-change, and human-merge if check_diff.py's window check would
    refuse it.
  - needs-human findings: one issue per tool and reason, closed with a comment
    on the first run where the problem is gone.
  - automated/claim/* and automated/window branches without an open pull
    request (one closed by hand) are deleted.
bookkeeping: one pull request from automated/bookkeeping with every changed
  data file, after check_diff.py's bookkeeping check passes on them. Tools
  with a claim change or a problem waiting (in the report, or in an open claim
  pull request) are left out: refreshing their file would conflict with that
  pull request, and their older date shows on the site until it is resolved.
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import findings as block  # noqa: E402
import ghapi  # noqa: E402
from check_diff import KUBERNETES_FILE, TOOL_FILE, check_bookkeeping, check_claim, check_window  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LABELS = {
    "claim-change": ("1d76db", "Upstream changed a compatibility claim"),
    "window-change": ("5319e7", "The Kubernetes window changed"),
    "bookkeeping": ("c5def5", "Only verified dates and source commits moved"),
    "human-merge": ("d93f0b", "Needs a maintainer to review and merge"),
    "needs-human": ("b60205", "Automation could not read or resolve an upstream statement"),
}
ALLOWED_PATHS = ("data/tools/", "data/kubernetes.yaml")


# --- GitHub operations --------------------------------------------------------

def ensure_labels():
    existing = {label["name"] for label in ghapi.paged(f"repos/{ghapi.repo()}/labels")}
    for name, (color, description) in LABELS.items():
        if name not in existing:
            ghapi.call("POST", f"repos/{ghapi.repo()}/labels", {"name": name, "color": color, "description": description})


def commit_files(base, files, message):
    """A commit on top of base that sets each path to its text. Returns its SHA."""
    for path in files:
        if not path.startswith(ALLOWED_PATHS):
            raise SystemExit(f"refusing to commit {path}: only data/ files are published")
    repo = ghapi.repo()
    base_tree = ghapi.get(f"repos/{repo}/git/commits/{base}")["tree"]["sha"]
    tree = []
    for path, text in sorted(files.items()):
        blob = ghapi.call("POST", f"repos/{repo}/git/blobs", {
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"), "encoding": "base64"})
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    new_tree = ghapi.call("POST", f"repos/{repo}/git/trees", {"base_tree": base_tree, "tree": tree})["sha"]
    return ghapi.call("POST", f"repos/{repo}/git/commits", {"message": message, "tree": new_tree, "parents": [base]})["sha"]


def set_branch(branch, sha):
    repo = ghapi.repo()
    try:
        ghapi.call("PATCH", f"repos/{repo}/git/refs/heads/{branch}", {"sha": sha, "force": True})
    except ghapi.GitHubError as err:
        if err.status not in (404, 422):
            raise
        ghapi.call("POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})


def delete_branch(branch):
    try:
        ghapi.call("DELETE", f"repos/{ghapi.repo()}/git/refs/heads/{branch}")
    except ghapi.GitHubError as err:
        if err.status not in (404, 422):
            raise


def open_pull(branch):
    owner = ghapi.repo().split("/")[0]
    pulls = ghapi.get(f"repos/{ghapi.repo()}/pulls", state="open", head=f"{owner}:{branch}")
    return pulls[0] if pulls else None


def upsert_pull(branch, title, body, labels, rewrite, base, files, message):
    """Open or update the pull request from branch. Rewrites the branch only when asked to."""
    repo = ghapi.repo()
    pull = open_pull(branch)
    if pull and not rewrite:
        if pull["title"] != title or pull["body"] != body:
            ghapi.call("PATCH", f"repos/{repo}/pulls/{pull['number']}", {"title": title, "body": body})
        print(f"{branch}: unchanged, pull request #{pull['number']} kept")
        return pull["number"]
    set_branch(branch, commit_files(base, files, message))
    if pull:
        ghapi.call("PATCH", f"repos/{repo}/pulls/{pull['number']}", {"title": title, "body": body})
        number = pull["number"]
        print(f"{branch}: rewritten, pull request #{number} updated")
    else:
        default = ghapi.get(f"repos/{repo}")["default_branch"]
        number = ghapi.call("POST", f"repos/{repo}/pulls", {"title": title, "head": branch, "base": default, "body": body})["number"]
        print(f"{branch}: pull request #{number} opened")
    ghapi.call("POST", f"repos/{repo}/issues/{number}/labels", {"labels": labels})
    return number


def is_ours(item):
    """Opened by the automation App, so automation may close it."""
    return item["user"]["login"] == os.environ.get("AUTOMATION_LOGIN", "k8s-m8s-automation[bot]")


def close_pull(pull, comment):
    repo = ghapi.repo()
    ghapi.call("POST", f"repos/{repo}/issues/{pull['number']}/comments", {"body": comment})
    ghapi.call("PATCH", f"repos/{repo}/pulls/{pull['number']}", {"state": "closed"})
    delete_branch(pull["head"]["ref"])
    print(f"{pull['head']['ref']}: closed #{pull['number']}")


# --- pull request text --------------------------------------------------------

def permalink(item, commit=None):
    """The finding's source at its recorded commit (or at commit), where the source is a file in a GitHub repository."""
    source, commit = item.get("source") or "", commit or item.get("source_commit")
    if commit and "/blob/" in source:
        parts = source.split("/blob/", 1)
        return f"{parts[0]}/blob/{commit}/{parts[1].split('/', 1)[1]}"
    if commit and source.startswith("https://github.com/") and "#" in source and source.count("/") == 4:
        repo, anchor = source.split("#", 1)
        return f"{repo}/blob/{commit}/README.md#{anchor}"
    return source


def value(entry, field):
    if not entry or field not in entry:
        return "–"
    return "`" + json.dumps(entry[field], sort_keys=True).replace("|", "\\|").replace("`", "'") + "`"


def source_links(item):
    links = f"[at {str(item.get('source_commit') or '')[:7]}]({permalink(item)})"
    if item.get("removed_commit"):
        links += f", gone [at {item['removed_commit'][:7]}]({permalink(item, item['removed_commit'])})"
    elif "removed_commit" in item:
        links += ", no commit read today shows the removal"
    return links


def change_table(items):
    lines = ["| Entry | Field | Old | New | Source |", "|---|---|---|---|---|"]
    for item in items:
        fields = item["fields"] or ["(entry)"]
        for field in fields:
            lines.append(f"| {item['version']} | {field} | {value(item['old'], field)} | {value(item['new'], field)} | "
                         f"{source_links(item)} |")
    return "\n".join(lines)


def claim_body(tool, items, test=False, problems=()):
    note = ("**Test fixture.** This pull request contains a deliberate mistake, to check that the review "
            "marks it `mismatch` and that it is not merged. Close it once checked.\n\n") if test else ""
    return (
        f"{note}Opened by `detect.yml`: upstream's statement for **{tool}** no longer matches the recorded data.\n\n"
        + change_table(items) + "\n\n"
        + (f"This change needs a maintainer to review and merge it: {'; '.join(problems)}.\n\n" if problems
           else "An automated review checks each change against the source at the linked commit. "
                "It merges only if every gate in `docs/maintainers/automation.md` passes.\n\n")
        + block.render(items)
    )


# --- commands -------------------------------------------------------------------

def read_data(data_dir, path):
    return (Path(data_dir) / path).read_text(encoding="utf-8")


def gate_problems(check, path, data_dir, items):
    """What a check_diff.py check would refuse in the change to path, compared with main as the merge gates compare it."""
    def read_main(p):
        return (ROOT / p).read_text(encoding="utf-8") if (ROOT / p).is_file() else None
    return check(read_main, lambda p: read_data(data_dir, p) if p == path else read_main(p), [path], items)


def group(items, key):
    groups = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    return groups


def needs_human_title(item):
    return f"[needs-human] {item['tool']}: {item.get('reason') or 'unresolved'}"


def publish_claims(report, data_dir, base):
    claims = group([f for f in report["findings"] if f["class"] == "claim"], lambda f: f["tool"])
    for tool, items in sorted(claims.items()):
        branch = f"automated/claim/{tool}"
        path = f"data/tools/{tool}.yaml"
        if not TOOL_FILE.fullmatch(path):
            raise SystemExit(f"unexpected tool name {tool!r}")
        test = bool(report.get("test_fixture"))
        title = f"{'[test fixture] ' if test else ''}{tool}: upstream claim change ({len(items)} entr{'y' if len(items) == 1 else 'ies'})"
        problems = gate_problems(check_claim, path, data_dir, items)
        body = claim_body(tool, items, test, problems)
        labels = ["claim-change"] + (["human-merge"] if problems else [])
        pull = open_pull(branch)
        rewrite = True
        if pull:
            try:
                same = block.comparable(block.parse(pull["body"])) == block.comparable(items)
            except ValueError:
                same = False
            state = ghapi.get(f"repos/{ghapi.repo()}/pulls/{pull['number']}").get("mergeable_state")
            rewrite = not same or state == "dirty"
        upsert_pull(branch, title, body, labels, rewrite, base, {path: read_data(data_dir, path)},
                    f"{tool}: take over upstream's changed claims\n\nOpened by detect.yml. See the pull request for sources.")
    for tool in report["checked"]:
        if tool in claims or tool == "kubernetes":
            continue
        pull = open_pull(f"automated/claim/{tool}")
        if pull and is_ours(pull) and pull["head"]["repo"]["full_name"] == ghapi.repo():
            close_pull(pull, "Upstream now matches the recorded data for this tool, so this change is no longer needed.")


def publish_window(report, data_dir, base):
    items = [f for f in report["findings"] if f["class"] == "window"]
    if not items:
        if "kubernetes" in report["checked"]:
            pull = open_pull("automated/window")
            if pull and is_ours(pull):
                close_pull(pull, "The recorded Kubernetes window matches upstream again.")
        return
    pull = open_pull("automated/window")
    rewrite = True
    if pull:
        try:
            rewrite = block.comparable(block.parse(pull["body"])) != block.comparable(items)
        except ValueError:
            pass
    problems = gate_problems(check_window, KUBERNETES_FILE, data_dir, items)
    body = (
        "Opened by `detect.yml`: the latest three Kubernetes minors changed.\n\n"
        + change_table(items) + "\n\n"
        + (f"This change needs a maintainer: {'; '.join(problems)}. Follow the window roll runbook in `CONTRIBUTING.md`.\n\n"
           if problems else
           "It merges automatically once Kubernetes publishes the new minor's release and every gate in "
           "`docs/maintainers/automation.md` passes.\n\n")
        + block.render(items)
    )
    upsert_pull("automated/window", f"Kubernetes window: {', '.join(items[0]['new']['window'])}", body,
                ["window-change"] + (["human-merge"] if problems else []), rewrite, base,
                {KUBERNETES_FILE: read_data(data_dir, KUBERNETES_FILE)},
                "Update the Kubernetes window\n\nOpened by detect.yml.")


def publish_issues(report):
    repo = ghapi.repo()
    wanted = group([f for f in report["findings"] if f["class"] == "needs-human"], needs_human_title)
    attempted = set(report["checked"]) | {f["tool"] for f in report["findings"] if f["class"] == "needs-human"}
    existing = {i["title"]: i for i in ghapi.paged(f"repos/{repo}/issues", state="open", labels="needs-human")
                if "pull_request" not in i}
    for title, items in sorted(wanted.items()):
        body = ("Opened by `detect.yml`. Nothing was changed for this tool; a maintainer needs to look.\n\n"
                + "\n".join(f"- {i['message']}" for i in items)
                + "\n\nThis issue closes itself on the first daily run where the problem is gone.\n")
        if title in existing:
            if existing[title]["body"] != body:
                ghapi.call("PATCH", f"repos/{repo}/issues/{existing[title]['number']}", {"body": body})
            print(f"issue kept: {title}")
        else:
            number = ghapi.call("POST", f"repos/{repo}/issues", {"title": title, "body": body, "labels": ["needs-human"]})["number"]
            print(f"issue #{number} opened: {title}")
    for title, issue in existing.items():
        tool = title.removeprefix("[needs-human] ").split(":", 1)[0]
        if title not in wanted and tool in attempted and is_ours(issue):
            ghapi.call("POST", f"repos/{repo}/issues/{issue['number']}/comments",
                       {"body": "The daily run no longer reports this problem, so this issue is closed."})
            ghapi.call("PATCH", f"repos/{repo}/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
            print(f"issue #{issue['number']} closed: {title}")


def waiting_tools(report):
    """Tools with a claim change or a problem waiting: in the report, or in an open claim pull request."""
    tools = {f["tool"] for f in report["findings"] if f["class"] in ("claim", "needs-human")}
    for pull in ghapi.paged(f"repos/{ghapi.repo()}/pulls", state="open"):
        if pull["head"]["ref"].startswith("automated/claim/"):
            tools.add(pull["head"]["ref"].removeprefix("automated/claim/"))
    return tools


def publish_bookkeeping(report, data_dir, base):
    waiting = waiting_tools(report)
    files, left_out = {}, []
    for file in sorted((Path(data_dir) / "data").rglob("*.yaml")):
        path = file.relative_to(data_dir).as_posix()
        tool = TOOL_FILE.fullmatch(path)
        if tool and tool.group(1) in waiting:
            left_out.append(tool.group(1))
            continue
        before = (ROOT / path).read_text(encoding="utf-8") if (ROOT / path).is_file() else None
        text = file.read_text(encoding="utf-8")
        if before != text:
            files[path] = text
    if not files:
        print("bookkeeping: nothing changed")
        return
    problems = check_bookkeeping(lambda p: (ROOT / p).read_text(encoding="utf-8") if (ROOT / p).is_file() else None,
                                 lambda p: files.get(p), sorted(files))
    if problems:
        raise SystemExit("bookkeeping changes failed the check:\n" + "\n".join(problems))
    moved = [f for f in report["findings"] if f["class"] == "bookkeeping" and f["tool"] not in waiting]
    body = (
        "Opened by `detect.yml`: every entry that still matches upstream gets today's `verified` date, "
        "and `source_commit` where upstream edited the file without changing the claim.\n\n"
        f"{len(files)} file(s) changed, {len(moved)} source commit(s) moved. "
        + (f"Left out until their change or problem is resolved: {', '.join(left_out)}. " if left_out else "")
        + "`automerge.yml` merges this after `scripts/check_diff.py bookkeeping` passes on the pull request.\n"
    )
    upsert_pull("automated/bookkeeping", f"Bookkeeping: verified {report['date']}", body, ["bookkeeping"], True,
                base, files, f"Refresh verified dates ({report['date']})\n\nOpened by detect.yml.")


def stale_branches():
    """automated/claim/* and automated/window branches with no open pull request (for example, one closed by hand)."""
    repo = ghapi.repo()
    refs = ghapi.get(f"repos/{repo}/git/matching-refs/heads/automated/")
    branches = [r["ref"].removeprefix("refs/heads/") for r in refs]
    branches = [b for b in branches if b.startswith("automated/claim/") or b == "automated/window"]
    open_heads = {p["head"]["ref"] for p in ghapi.paged(f"repos/{repo}/pulls", state="open")}
    return [b for b in branches if b not in open_heads]


def prune_branches():
    for branch in stale_branches():
        delete_branch(branch)
        print(f"{branch}: deleted, no open pull request")


def pending(report):
    """Whether the publish job has anything to do: something to open, or something open to close."""
    if any(f["class"] in ("claim", "window", "needs-human") for f in report["findings"]):
        return True
    repo = ghapi.repo()
    if ghapi.get(f"repos/{repo}/issues", state="open", labels="needs-human", per_page=1):
        return True
    if any(p["head"]["ref"].startswith(("automated/claim/", "automated/window"))
           for p in ghapi.paged(f"repos/{repo}/pulls", state="open")):
        return True
    return bool(stale_branches())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["detect", "bookkeeping", "pending"])
    parser.add_argument("--report", required=True)
    parser.add_argument("--data-dir")
    parser.add_argument("--base", help="the main commit the data files were computed from")
    args = parser.parse_args()
    with open(args.report, encoding="utf-8") as handle:
        report = json.load(handle)

    if args.command == "pending":
        print("true" if pending(report) else "false")
        return 0
    if not (args.data_dir and args.base):
        parser.error("detect and bookkeeping need --data-dir and --base")
    ensure_labels()
    if args.command == "detect":
        publish_claims(report, args.data_dir, args.base)
        publish_window(report, args.data_dir, args.base)
        publish_issues(report)
        prune_branches()
    else:
        publish_bookkeeping(report, args.data_dir, args.base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
