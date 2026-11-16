#!/usr/bin/env python3
"""Validate k8s-m8s data files.

Usage:
    python scripts/validate.py            # schema and consistency checks
    python scripts/validate.py --strict   # also require source_commit everywhere
"""

import argparse
import datetime
import json
import sys
from collections import Counter

from jsonschema import Draft202012Validator

from common import (
    KUBERNETES_FILE,
    SCHEMA_FILE,
    is_unsupported,
    load_yaml,
    minor_of,
    release_label,
    tool_files,
    version_key,
)


def check_kubernetes(strict):
    errors = []
    data = load_yaml(KUBERNETES_FILE)
    window = data.get("window") if isinstance(data, dict) else None
    if not isinstance(window, list) or len(window) != 3:
        return ["window must list exactly three Kubernetes minors"]
    if not all(isinstance(v, str) for v in window):
        return ["window versions must be quoted strings"]
    ordered = sorted(window, key=version_key, reverse=True)
    if len(set(window)) != 3:
        errors.append("window versions must be unique")
    if strict and not data.get("verified"):
        errors.append("verified date is required in strict mode")
    if window != ordered:
        errors.append("window must be ordered newest first")
    return errors


def is_under(url, prefix):
    """True if url is the prefix itself or sits beneath it (path, query or fragment)."""
    url, prefix = url.lower(), prefix.lower().rstrip("/")
    if not url.startswith(prefix):
        return False
    rest = url[len(prefix):]
    return rest == "" or rest[0] in "/#?"


def date_problem(value):
    """Why a date string is unusable, or None. The schema only checks its shape."""
    if value is None:
        return None
    try:
        date = datetime.date.fromisoformat(value)
    except ValueError:
        return f"'{value}' is not a real date"
    if date > datetime.date.today():
        return f"'{value}' is in the future"
    return None


def band_bounds(release):
    """(low, high) version keys covered by a release entry; None means unbounded."""
    if "version" in release:
        key = version_key(release["version"])[:2]
        return key, key
    band = release["version_range"]
    low = version_key(band["min"])[:2] if "min" in band else None
    high = version_key(band["max"])[:2] if "max" in band else None
    return low, high


def bands_overlap(a, b):
    (a_low, a_high), (b_low, b_high) = band_bounds(a), band_bounds(b)
    a_before_b = a_high is not None and b_low is not None and a_high < b_low
    b_before_a = b_high is not None and a_low is not None and b_high < a_low
    return not (a_before_b or b_before_a)


def check_tool(path, validator, strict):
    try:
        data = load_yaml(path)
    except Exception as exc:  # malformed YAML
        return [f"cannot parse YAML: {exc}"]

    errors = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    if errors:
        return errors

    if data["name"] != path.stem:
        errors.append(f"name '{data['name']}' does not match file name '{path.stem}'")

    counts = Counter(r["version"] for r in data["releases"] if "version" in r)
    for version, count in counts.items():
        if count > 1:
            errors.append(f"version {version} appears {count} times; one entry per version")

    releases = data["releases"]
    for index, release in enumerate(releases):
        band = release.get("version_range")
        if not band:
            continue
        if "min" in band and "max" in band and version_key(band["min"]) > version_key(band["max"]):
            errors.append(f"release {release_label(release)}: version_range.min is newer than max")
        for other_index, other in enumerate(releases):
            # compare with every single version, and with each other band once
            if other_index == index or ("version_range" in other and other_index < index):
                continue
            if bands_overlap(release, other):
                errors.append(
                    f"release {release_label(release)} overlaps release {release_label(other)}; "
                    "one entry per version"
                )

    allowed = [data["repo"], *data.get("official_sources", [])]

    for release in data["releases"]:
        label = f"release {release_label(release)}"
        if not any(is_under(release["source"], prefix) for prefix in allowed):
            errors.append(
                f"{label}: source {release['source']} is not under the tool's repo "
                "or its official_sources"
            )
        has_claim = any(release.get(k) for k in ("tested", "stated", "unsupported"))
        if not has_claim:
            errors.append(f"{label}: no claim recorded; omit the release if upstream says nothing")

        tested_minors = {minor_of(p) for p in release.get("tested", [])}
        # 'latest' in an unsupported range covers any tested minor above its min
        overlap = {m for m in tested_minors if is_unsupported(release, m, m)}
        if overlap:
            errors.append(f"{label}: {sorted(overlap)} listed as both tested and unsupported")

        stated = release.get("stated")
        if stated and "min" in stated and stated["max"] != "latest":
            if version_key(stated["min"]) > version_key(stated["max"]):
                errors.append(f"{label}: stated.min is newer than stated.max")

        for field in ("verified", "delisted"):
            problem = date_problem(release.get(field))
            if problem:
                errors.append(f"{label}: {field} {problem}")

        if strict and "source_commit" not in release:
            errors.append(f"{label}: source_commit is required in strict mode")

    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="require source_commit and verified dates")
    args = parser.parse_args()

    with open(SCHEMA_FILE, encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    failures = 0

    for message in check_kubernetes(args.strict):
        print(f"data/kubernetes.yaml: {message}")
        failures += 1

    files = tool_files()
    if not files:
        print("data/tools: no tool files found")
        failures += 1

    for path in files:
        for message in check_tool(path, validator, args.strict):
            print(f"data/tools/{path.name}: {message}")
            failures += 1

    if failures:
        print(f"\n{failures} problem(s) found")
        return 1
    print(f"OK: {len(files)} tool file(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
