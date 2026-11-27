#!/usr/bin/env python3
"""Re-read every upstream statement and compare it with the recorded data.

Usage:
    python scripts/verify.py                # report differences for all tools
    python scripts/verify.py velero rook    # only these tools (or --tools velero,rook)
    python scripts/verify.py --write        # also refresh verified/source_commit
                                            # on entries that still match upstream
    python scripts/verify.py --apply        # also write upstream's new, changed and
                                            # removed rows into the data files
    python scripts/verify.py --claims-only  # write only the entries whose claims changed,
                                            # and the Kubernetes window if it changed
    python scripts/verify.py --claims-only --bookkeeping-out DIR
                                            # also write, from the same reading, what --write
                                            # would make of the data into DIR/data
    python scripts/verify.py --detect-only --report-json report.json
                                            # change nothing, write a machine-readable report

Without --apply or --claims-only, differences are only reported. With them they
are written to the data files as a proposal: every change is still checked
against the linked sources before it is merged. Set GITHUB_TOKEN (or GH_TOKEN)
to avoid GitHub API rate limits.

With --report-json the exit status is 0 whatever was found, because the caller
reads the report; it is non-zero only if the script itself failed.

Each finding in the report has a class:
    bookkeeping  only source_commit moved; every claim field is identical
    claim        a claim field changed, or an entry was added, removed or delisted
    window       the Kubernetes window differs from data/kubernetes.yaml
    needs-human  the statement could not be read or parsed, or cannot be resolved
                 automatically (for example a delisted row that upstream lists again)
Refreshing `verified` alone is not reported as a finding.
"""

import argparse
import copy
import datetime
import json
import re
import shutil
import sys
from pathlib import Path

from common import KUBERNETES_FILE, load_yaml, release_label, tool_files, version_key, write_releases
from extractors import EXTRACTORS, UpstreamChanged, kubernetes_window

CLAIM_FIELDS = ("tested", "stated", "unsupported", "source")
MODES = ("report", "write", "apply", "claims")


def claims(release):
    """The parts of a release entry that must match upstream, in comparable form."""
    result = {field: release.get(field) for field in CLAIM_FIELDS if release.get(field)}
    if "tested" in result:
        result["tested"] = sorted(result["tested"], key=version_key)
    return result


def snapshot(release):
    """An entry as a finding shows it: its key, claims, commit and delisted date."""
    if release is None:
        return None
    key = {"version": release["version"]} if "version" in release else {"version_range": release["version_range"]}
    extra = {k: release[k] for k in ("source_commit", "delisted") if k in release}
    return {**key, **claims(release), **extra}


def changed_fields(old, new):
    old, new = old or {}, new or {}
    return [f for f in (*CLAIM_FIELDS, "delisted") if old.get(f) != new.get(f)]


def describe(recorded, upstream):
    parts = []
    for field in CLAIM_FIELDS:
        if recorded.get(field) != upstream.get(field):
            parts.append(f"{field}: recorded {recorded.get(field)!r}, upstream {upstream.get(field)!r}")
    return "; ".join(parts)


def finding(tool, label, cls, message, old=None, new=None, fields=None, reason=None, removal=False, removed_commit=None):
    """One classified finding. A removal (an entry becoming delisted) also names
    `removed_commit`: the upstream commit where the row is gone, or None if no
    commit read today shows it."""
    after = new or old or {}
    result = {
        "id": f"{tool}/{label}",
        "tool": tool,
        "version": label,
        "class": cls,
        "fields": fields if fields is not None else changed_fields(old, new),
        "source": after.get("source"),
        "source_commit": after.get("source_commit"),
        "old": old,
        "new": new,
        "message": message,
    }
    if removal:
        result["removed_commit"] = removed_commit
    if reason:
        result["reason"] = reason
    return result


def current_commit(upstream, source):
    """The commit at which today's reading of `source` was made, if the rows read from it agree on one."""
    commits = {r.get("source_commit") for r in upstream if r.get("source") == source and "delisted" not in r}
    return commits.pop() if len(commits) == 1 else None


def reconcile(releases, upstream, today, mode, tool="tool", findings=None):
    """Compare recorded releases with upstream's, updating them in place as mode allows.

    mode "report" changes nothing, "write" refreshes verified and source_commit
    on entries that still match, "apply" also takes over upstream's changed,
    new and removed rows, and "claims" takes over those rows without touching
    entries that still match. An upstream row that carries `delisted` was read
    from the source's history: it is compared like any other row, including
    its delisted date. Returns (matching, differences, problems): the number
    of current entries that match, messages for differences (applied in
    "apply" and "claims" mode), and messages for things only a human can
    resolve. If `findings` is a list, a classified finding is appended to it
    for every difference, problem and moved source_commit.
    """
    findings = [] if findings is None else findings
    applying = mode in ("apply", "claims")
    refreshing = mode in ("write", "apply")
    by_label = {release_label(r): r for r in upstream}
    matching, differences, problems = 0, [], []

    def differ(label, message, release, found):
        differences.append(message)
        before = snapshot(release)
        removal = "delisted" in found and "delisted" not in release
        if applying:
            for field in CLAIM_FIELDS:
                release.pop(field, None)
            release.update(claims(found), source_commit=found["source_commit"], verified=today)
            if "delisted" in found:
                release["delisted"] = found["delisted"]
        findings.append(finding(tool, label, "claim", message, before, snapshot(found),
                                removal=removal, removed_commit=found.get("removed_commit") if removal else None))

    def bookkeep(label, release, found):
        if release.get("source_commit") != found["source_commit"]:
            findings.append(finding(
                tool, label, "bookkeeping", f"{label}: source_commit moved, claims unchanged",
                snapshot(release), snapshot({**release, "source_commit": found["source_commit"]}),
                fields=["source_commit"],
            ))
        if refreshing:
            release.update(source_commit=found["source_commit"], verified=today)

    for release in releases:
        label = release_label(release)
        found = by_label.pop(label, None)
        if found and "delisted" in found:
            if claims(release) == claims(found) and release.get("delisted") == found["delisted"]:
                bookkeep(label, release, found)
                continue
            if "delisted" in release:
                message = (f"{label}: upstream history differs ({describe(claims(release), claims(found))}; "
                           f"delisted: recorded {release['delisted']}, upstream {found['delisted']})")
            else:
                message = (f"{label}: no longer listed upstream; last values taken from the commit "
                           f"that last listed it, delisted {found['delisted']}")
            differ(label, message, release, found)
        elif "delisted" in release:
            if found:
                message = f"{label}: marked delisted but upstream lists it again"
                problems.append(message)
                findings.append(finding(tool, label, "needs-human", message, snapshot(release), snapshot(found),
                                        reason="delisted row listed again"))
        elif not found:
            before = snapshot(release)
            if applying:
                release["delisted"] = today
                message = f"{label}: no longer listed upstream; marked delisted, last values kept"
            else:
                message = f"{label}: no longer listed upstream (add 'delisted' and keep the values)"
            differences.append(message)
            findings.append(finding(tool, label, "claim", message, before, {**before, "delisted": today},
                                    fields=["delisted"], removal=True,
                                    removed_commit=current_commit(upstream, release.get("source"))))
        elif claims(release) != claims(found):
            differ(label, f"{label}: changed upstream ({describe(claims(release), claims(found))})", release, found)
        else:
            matching += 1
            bookkeep(label, release, found)
    for label in sorted(by_label):
        found = by_label[label]
        if "delisted" in found:
            message = f"{label}: in upstream's history (delisted {found['delisted']}) but not recorded"
        else:
            message = f"{label}: listed upstream but not recorded"
        differences.append(message)
        findings.append(finding(tool, label, "claim", message, None, snapshot(found)))
        if applying:
            row = {k: v for k, v in found.items() if k != "removed_commit"}  # the review's evidence, not data
            releases.append({**row, **claims(found), "verified": today})
    return matching, differences, problems


def verify_window(mode, today, findings=None, fetch=kubernetes_window, path=KUBERNETES_FILE):
    """Compare the recorded Kubernetes window with upstream's, writing it as mode allows."""
    data = load_yaml(path)
    recorded = data["window"]
    window, sha = fetch()
    differences = [] if recorded == window else [f"window: recorded {recorded}, upstream {window}"]
    if differences and findings is not None:
        findings.append({
            "id": "kubernetes/window", "tool": "kubernetes", "version": "window", "class": "window",
            "fields": ["window"], "source": data.get("source"), "source_commit": sha,
            "old": {"window": recorded, "source_commit": data.get("source_commit")},
            "new": {"window": window, "source_commit": sha}, "message": differences[0],
        })
    elif not differences and findings is not None and data.get("source_commit") != sha:
        findings.append({
            "id": "kubernetes/window", "tool": "kubernetes", "version": "window", "class": "bookkeeping",
            "fields": ["source_commit"], "source": data.get("source"), "source_commit": sha,
            "old": {"window": recorded, "source_commit": data.get("source_commit")},
            "new": {"window": window, "source_commit": sha}, "message": "window: source_commit moved",
        })
    if mode == "apply" or (mode == "claims" and differences) or (mode == "write" and not differences):
        text = path.read_text(encoding="utf-8")
        listed = ", ".join(f'"{minor}"' for minor in window)
        text = re.sub(r"(?m)^window:.*$", f"window: [{listed}]", text)
        text = re.sub(r"(?m)^source_commit:.*$", f"source_commit: {sha}", text)
        text = re.sub(r"(?m)^verified:.*$", f"verified: {today}", text)
        path.write_text(text, encoding="utf-8")
    return differences


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tools", nargs="*", help="tool names to check (default: all, plus the Kubernetes window)")
    parser.add_argument("--tools", dest="tool_list", default="", help="comma-separated tool names, as an alternative")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true", help="refresh verified and source_commit on matching entries")
    group.add_argument("--apply", action="store_true", help="also write upstream's differences into the data files")
    group.add_argument("--claims-only", action="store_true",
                       help="write only entries whose claims changed (and the window if it changed)")
    group.add_argument("--detect-only", action="store_true", help="change nothing (the default); for clarity in workflows")
    parser.add_argument("--report-json", metavar="PATH", help="write the classified findings to PATH as JSON")
    parser.add_argument("--bookkeeping-out", metavar="DIR",
                        help="with --claims-only: also write the bookkeeping refresh (what --write would write) "
                             "of the same reading to DIR/data, so upstream is read once for both")
    args = parser.parse_args()
    mode = "apply" if args.apply else "write" if args.write else "claims" if args.claims_only else "report"
    if args.bookkeeping_out and mode != "claims":
        parser.error("--bookkeeping-out needs --claims-only")
    today = datetime.date.today().isoformat()
    selected = list(args.tools) + [t for t in args.tool_list.split(",") if t.strip()]
    selected = [t.strip() for t in selected]
    difference_count = problem_count = 0
    findings, checked = [], []

    known = {path.stem for path in tool_files()}
    unknown = sorted(set(selected) - known)
    if unknown:
        parser.error(f"no data file for: {', '.join(unknown)}")
    bookkeeping = None
    if args.bookkeeping_out:  # a copy of the data as it was before this run, refreshed below
        bookkeeping = Path(args.bookkeeping_out) / "data"
        shutil.copytree(KUBERNETES_FILE.parent, bookkeeping, dirs_exist_ok=True)

    def report(name, summary, differences=(), problems=()):
        nonlocal difference_count, problem_count
        difference_count += len(differences)
        problem_count += len(problems)
        print(f"{name}: {summary}")
        for message in differences:
            print(f"  - {'applied: ' if mode in ('apply', 'claims') else ''}{message}")
        for message in problems:
            print(f"  - needs a human: {message}")

    def broken(name, label, reason, message):
        findings.append(finding(name, label, "needs-human", message, fields=[], reason=reason))
        report(name, "error", problems=[message])

    if not selected:
        try:
            fetched = kubernetes_window()
            if bookkeeping:
                verify_window("write", today, None, fetch=lambda: fetched, path=bookkeeping / KUBERNETES_FILE.name)
            differences = verify_window(mode, today, findings, fetch=lambda: fetched, path=KUBERNETES_FILE)
            checked.append("kubernetes")
            report("kubernetes", "window differs" if differences else "window matches", differences)
        except UpstreamChanged as exc:
            broken("kubernetes", "window", "statement changed shape", f"release schedule changed shape: {exc}")
        except Exception as exc:  # network failure
            broken("kubernetes", "window", "could not read upstream", f"could not read upstream: {exc}")

    for path in tool_files():
        if selected and path.stem not in selected:
            continue
        data = load_yaml(path)
        if path.stem not in EXTRACTORS:
            broken(path.stem, "extractor", "no extractor", "no extractor in scripts/extractors.py")
            continue
        try:
            upstream = EXTRACTORS[path.stem]()
        except UpstreamChanged as exc:
            broken(path.stem, "extractor", "statement changed shape",
                   f"statement changed shape, re-read it by hand: {exc}")
            continue
        except Exception as exc:  # network failure
            broken(path.stem, "extractor", "could not read upstream", f"could not read upstream: {exc}")
            continue
        checked.append(path.stem)
        listed = sum(1 for release in data["releases"] if "delisted" not in release)
        if bookkeeping:  # the same reading, as --write would apply it; its findings are the same as below
            refreshed = copy.deepcopy(data["releases"])
            reconcile(refreshed, copy.deepcopy(upstream), today, "write", path.stem)
            write_releases(bookkeeping / path.parent.name / path.name, refreshed)
        matching, differences, problems = reconcile(data["releases"], upstream, today, mode, path.stem, findings)
        if mode != "report":
            write_releases(path, data["releases"])
        report(path.stem, f"{matching} of {listed} current entries match upstream", differences, problems)

    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as handle:
            json.dump({"date": today, "mode": mode, "checked": checked, "findings": findings}, handle, indent=2)
            handle.write("\n")
        counts = {}
        for item in findings:
            counts[item["class"]] = counts.get(item["class"], 0) + 1
        print(f"\nreport written to {args.report_json}: " + (", ".join(f"{n} {c}" for c, n in sorted(counts.items())) or "no findings"))
        return 0
    if mode in ("apply", "claims"):
        print(f"\n{difference_count} difference(s) applied for review, {problem_count} problem(s) need a human")
        return 1 if problem_count else 0
    if difference_count or problem_count:
        print(f"\n{difference_count + problem_count} difference(s) need review")
        return 1
    print("\nOK: recorded data matches upstream")
    return 0


if __name__ == "__main__":
    sys.exit(main())
