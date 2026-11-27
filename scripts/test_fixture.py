#!/usr/bin/env python3
"""Write a deliberately wrong claim change, to rehearse the review and merge gates end to end.

Usage:
    python scripts/test_fixture.py --tool velero --report-json report.json [--kind claim|removal]

claim (the default) adds a `tested` Kubernetes patch version that upstream
does not state to the tool's newest current entry. removal marks that entry
delisted, naming as the commit where the row is gone the very commit that
still lists it. Either way it writes a report with that one claim finding, as
verify.py would. detect.yml runs this instead of verify.py when its `fixture`
input is set. The review must mark the finding `mismatch`, and automerge.yml
must refuse it and add `human-merge`. Nothing here talks to GitHub.
"""

import argparse
import datetime
import json
import sys

from common import TOOLS_DIR, load_window, load_yaml, minor_of, ordered, release_label, write_releases
from verify import finding, snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--kind", choices=["claim", "removal"], default="claim")
    args = parser.parse_args()
    path = TOOLS_DIR / f"{args.tool}.yaml"
    if not path.is_file():
        parser.error(f"no data file for {args.tool}")
    releases = load_yaml(path)["releases"]
    current = [r for r in ordered(releases) if "delisted" not in r and r.get("source_commit")]
    if not current:
        parser.error(f"{args.tool} has no current entry with a source_commit")
    entry = current[0]
    before = snapshot(entry)
    label = release_label(entry)
    today = datetime.date.today().isoformat()
    if args.kind == "removal":
        if len(current) < 2:
            parser.error(f"{args.tool} needs two current entries, so one remains")
        entry["delisted"] = today  # upstream still lists it at the commit named below
        item = finding(args.tool, label, "claim",
                       f"{label}: test fixture marks it delisted, though upstream still lists it",
                       before, snapshot(entry), fields=["delisted"], removal=True, removed_commit=entry["source_commit"])
        planted = "delisted"
    else:
        newest = load_window()[0]
        planted = f"{newest}.99"  # no Kubernetes patch release is numbered like this
        if any(minor_of(v) == newest and v == planted for v in entry.get("tested", [])):
            parser.error("the planted version is already recorded")
        entry["tested"] = sorted({*entry.get("tested", []), planted}, key=lambda v: tuple(int(p) for p in v.split(".")))
        entry["verified"] = today
        item = finding(args.tool, label, "claim", f"{label}: test fixture adds tested {planted}, which upstream does not state",
                       before, snapshot(entry))
        planted = f"tested {planted}"
    write_releases(path, releases)
    with open(args.report_json, "w", encoding="utf-8") as handle:
        json.dump({"date": today, "mode": "fixture", "checked": [], "findings": [item], "test_fixture": True},
                  handle, indent=2)
        handle.write("\n")
    print(f"planted {planted} on {args.tool} {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
