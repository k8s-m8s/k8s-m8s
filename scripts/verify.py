#!/usr/bin/env python3
"""Re-read every upstream statement and compare it with the recorded data.

Usage:
    python scripts/verify.py                # report differences for all tools
    python scripts/verify.py velero rook    # only these tools
    python scripts/verify.py --write        # also refresh verified/source_commit
                                            # on entries that still match upstream
    python scripts/verify.py --apply        # also write upstream's new, changed and
                                            # removed rows into the data files

Without --apply, differences are only reported. With it they are written to
the data files as a proposal: a human must still read the diff against the
linked sources before it is merged. Set GITHUB_TOKEN (or GH_TOKEN) to avoid
GitHub API rate limits.
"""

import argparse
import datetime
import re
import sys

from common import KUBERNETES_FILE, load_yaml, release_label, tool_files, version_key, write_releases
from extractors import EXTRACTORS, UpstreamChanged, kubernetes_window

CLAIM_FIELDS = ("tested", "stated", "unsupported", "source")


def claims(release):
    """The parts of a release entry that must match upstream, in comparable form."""
    result = {field: release.get(field) for field in CLAIM_FIELDS if release.get(field)}
    if "tested" in result:
        result["tested"] = sorted(result["tested"], key=version_key)
    return result


def describe(recorded, upstream):
    parts = []
    for field in CLAIM_FIELDS:
        if recorded.get(field) != upstream.get(field):
            parts.append(f"{field}: recorded {recorded.get(field)!r}, upstream {upstream.get(field)!r}")
    return "; ".join(parts)


def reconcile(releases, upstream, today, mode):
    """Compare recorded releases with upstream's, updating them in place as mode allows.

    mode "report" changes nothing, "write" refreshes verified and source_commit
    on entries that still match, and "apply" also takes over upstream's changed,
    new and removed rows. An upstream row that carries `delisted` was read
    from the source's history: it is compared like any other row, including
    its delisted date. Returns (matching, differences, problems): the number
    of current entries that match, messages for differences (applied in
    "apply" mode), and messages for things only a human can resolve.
    """
    by_label = {release_label(r): r for r in upstream}
    matching, differences, problems = 0, [], []
    for release in releases:
        label = release_label(release)
        found = by_label.pop(label, None)
        if found and "delisted" in found:
            if claims(release) == claims(found) and release.get("delisted") == found["delisted"]:
                if mode != "report":
                    release.update(source_commit=found["source_commit"], verified=today)
                continue
            if "delisted" in release:
                differences.append(f"{label}: upstream history differs ({describe(claims(release), claims(found))}; "
                                   f"delisted: recorded {release['delisted']}, upstream {found['delisted']})")
            else:
                differences.append(f"{label}: no longer listed upstream; last values taken from the commit "
                                   f"that last listed it, delisted {found['delisted']}")
            if mode == "apply":
                for field in CLAIM_FIELDS:
                    release.pop(field, None)
                release.update(claims(found), source_commit=found["source_commit"],
                               delisted=found["delisted"], verified=today)
        elif "delisted" in release:
            if found:
                problems.append(f"{label}: marked delisted but upstream lists it again")
        elif not found:
            if mode == "apply":
                release["delisted"] = today
                differences.append(f"{label}: no longer listed upstream; marked delisted, last values kept")
            else:
                differences.append(f"{label}: no longer listed upstream (add 'delisted' and keep the values)")
        elif claims(release) != claims(found):
            differences.append(f"{label}: changed upstream ({describe(claims(release), claims(found))})")
            if mode == "apply":
                for field in CLAIM_FIELDS:
                    release.pop(field, None)
                release.update(claims(found), source_commit=found["source_commit"], verified=today)
        else:
            matching += 1
            if mode != "report":
                release.update(source_commit=found["source_commit"], verified=today)
    for label in sorted(by_label):
        if "delisted" in by_label[label]:
            differences.append(f"{label}: in upstream's history (delisted {by_label[label]['delisted']}) but not recorded")
        else:
            differences.append(f"{label}: listed upstream but not recorded")
        if mode == "apply":
            found = by_label[label]
            releases.append({**found, **claims(found), "verified": today})
    return matching, differences, problems


def verify_window(mode, today):
    recorded = load_yaml(KUBERNETES_FILE)["window"]
    window, sha = kubernetes_window()
    differences = [] if recorded == window else [f"window: recorded {recorded}, upstream {window}"]
    if mode == "apply" or (mode == "write" and not differences):
        text = KUBERNETES_FILE.read_text(encoding="utf-8")
        listed = ", ".join(f'"{minor}"' for minor in window)
        text = re.sub(r"(?m)^window:.*$", f"window: [{listed}]", text)
        text = re.sub(r"(?m)^source_commit:.*$", f"source_commit: {sha}", text)
        text = re.sub(r"(?m)^verified:.*$", f"verified: {today}", text)
        KUBERNETES_FILE.write_text(text, encoding="utf-8")
    return differences


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tools", nargs="*", help="tool names to check (default: all, plus the Kubernetes window)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true", help="refresh verified and source_commit on matching entries")
    group.add_argument("--apply", action="store_true", help="also write upstream's differences into the data files")
    args = parser.parse_args()
    mode = "apply" if args.apply else "write" if args.write else "report"
    today = datetime.date.today().isoformat()
    difference_count = problem_count = 0

    known = {path.stem for path in tool_files()}
    unknown = sorted(set(args.tools) - known)
    if unknown:
        parser.error(f"no data file for: {', '.join(unknown)}")

    def report(name, summary, differences=(), problems=()):
        nonlocal difference_count, problem_count
        difference_count += len(differences)
        problem_count += len(problems)
        print(f"{name}: {summary}")
        for message in differences:
            print(f"  - {'applied: ' if mode == 'apply' else ''}{message}")
        for message in problems:
            print(f"  - needs a human: {message}")

    if not args.tools:
        try:
            differences = verify_window(mode, today)
            report("kubernetes", "window differs" if differences else "window matches", differences)
        except Exception as exc:  # network failure or UpstreamChanged
            report("kubernetes", "error", problems=[f"could not read upstream: {exc}"])

    for path in tool_files():
        if args.tools and path.stem not in args.tools:
            continue
        data = load_yaml(path)
        if path.stem not in EXTRACTORS:
            report(path.stem, "not verifiable", problems=["no extractor in scripts/extractors.py"])
            continue
        try:
            upstream = EXTRACTORS[path.stem]()
        except UpstreamChanged as exc:
            report(path.stem, "error", problems=[f"statement changed shape, re-read it by hand: {exc}"])
            continue
        except Exception as exc:  # network failure
            report(path.stem, "error", problems=[f"could not read upstream: {exc}"])
            continue
        listed = sum(1 for release in data["releases"] if "delisted" not in release)
        matching, differences, problems = reconcile(data["releases"], upstream, today, mode)
        if mode != "report":
            write_releases(path, data["releases"])
        report(path.stem, f"{matching} of {listed} current entries match upstream", differences, problems)

    if mode == "apply":
        print(f"\n{difference_count} difference(s) applied for review, {problem_count} problem(s) need a human")
        return 1 if problem_count else 0
    if difference_count or problem_count:
        print(f"\n{difference_count + problem_count} difference(s) need review")
        return 1
    print("\nOK: recorded data matches upstream")
    return 0


if __name__ == "__main__":
    sys.exit(main())
