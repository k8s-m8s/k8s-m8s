"""Shared helpers for k8s-m8s scripts."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "data" / "tools"
KUBERNETES_FILE = ROOT / "data" / "kubernetes.yaml"
SCHEMA_FILE = ROOT / "schema" / "tool.schema.json"


class _Loader(yaml.SafeLoader):
    """SafeLoader that keeps dates as plain strings."""


_Loader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_yaml(path):
    with open(path, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_Loader)


def version_key(version):
    """Sort key for dotted numeric versions such as '1.37' or '1.37.1'."""
    return tuple(int(part) for part in version.split("."))


def minor_of(patch):
    return ".".join(patch.split(".")[:2])


def tool_files():
    return sorted(TOOLS_DIR.glob("*.yaml"))


def load_window():
    data = load_yaml(KUBERNETES_FILE)
    return sorted(data["window"], key=version_key, reverse=True)


def in_range(minor, rng, newest):
    """True if a Kubernetes minor falls in a {min?, max} range; 'latest' means newest."""
    upper = newest if rng["max"] == "latest" else rng["max"]
    if version_key(minor) > version_key(upper):
        return False
    return "min" not in rng or version_key(rng["min"]) <= version_key(minor)


def is_unsupported(release, minor, newest):
    for item in release.get("unsupported", []):
        if item == minor or (isinstance(item, dict) and in_range(minor, item, newest)):
            return True
    return False


def format_range(rng):
    if "min" not in rng:
        return f"up to {rng['max']}"
    if rng["min"] == rng["max"]:
        return rng["min"]
    return f"{rng['min']} to {rng['max']}"


def release_label(release):
    """Display name of a release entry: its version, or its band of versions."""
    if "version" in release:
        return release["version"]
    band = release["version_range"]
    if "min" in band and "max" in band:
        return f"{band['min']} to {band['max']}"
    if "min" in band:
        return f"{band['min']} and later"
    return f"{band['max']} and earlier"


def release_key(release):
    """Sort key: a band sorts by its upper bound, an open-ended band last."""
    if "version" in release:
        return version_key(release["version"])
    band = release["version_range"]
    return version_key(band["max"]) if "max" in band else (float("inf"),)


def ordered(releases):
    """Releases as the data files list them: current newest first, then delisted."""
    current = [r for r in releases if "delisted" not in r]
    delisted = [r for r in releases if "delisted" in r]
    return sorted(current, key=release_key, reverse=True) + sorted(delisted, key=release_key, reverse=True)


def _plain(value):
    """A string as a YAML scalar: unquoted when that is safe, otherwise double-quoted."""
    try:
        if yaml.safe_load(f"k: {value}") == {"k": value}:
            return value
    except yaml.YAMLError:
        pass
    return json.dumps(value, ensure_ascii=False)


def _quoted(version):
    return version if version == "latest" else f'"{version}"'


def _range_lines(rng, indent):
    lines = [f'{indent}min: "{rng["min"]}"'] if "min" in rng else []
    return lines + [f"{indent}max: {_quoted(rng['max'])}"]


def dump_releases(releases):
    """The `releases:` block of a tool file, in the one layout every data file uses."""
    lines = []
    for release in releases:
        if "version" in release:
            lines.append(f'  - version: "{release["version"]}"')
        else:
            lines.append("  - version_range:")
            lines += [f'      {bound}: "{release["version_range"][bound]}"' for bound in ("min", "max") if bound in release["version_range"]]
        if release.get("tested"):
            lines.append("    tested: [" + ", ".join(f'"{v}"' for v in release["tested"]) + "]")
        if release.get("stated"):
            lines.append("    stated:")
            lines += _range_lines(release["stated"], "      ")
            lines.append(f"      wording: {_plain(release['stated']['wording'])}")
        if release.get("unsupported"):
            lines.append("    unsupported:")
            for item in release["unsupported"]:
                if isinstance(item, str):
                    lines.append(f'      - "{item}"')
                else:
                    first, *rest = _range_lines(item, "        ")
                    lines += ["      - " + first.strip(), *rest]
        lines.append(f"    source: {release['source']}")
        if "source_commit" in release:
            lines.append(f"    source_commit: {release['source_commit']}")
        lines.append(f"    verified: {release['verified']}")
        if "delisted" in release:
            lines.append(f"    delisted: {release['delisted']}")
    return "\n".join(lines) + "\n"


def write_releases(path, releases):
    """Rewrite a tool file's releases, leaving its header and comments untouched."""
    head, marker, _ = path.read_text(encoding="utf-8").partition("\nreleases:\n")
    if not marker:
        raise ValueError(f"{path.name}: no 'releases:' line")
    path.write_text(head + marker + dump_releases(ordered(releases)), encoding="utf-8")


def cell(release, minor, window):
    """Return (level, detail) for one tool release on one Kubernetes minor.

    Levels, in order of precedence: tested, unsupported, stated, unknown.
    An open-ended stated range ('latest') covers up to the newest minor in the
    window and is never reported as tested.
    """
    tested = sorted(
        (p for p in release.get("tested", []) if minor_of(p) == minor),
        key=version_key,
    )
    if tested:
        return "tested", ", ".join(tested)
    newest = max(window, key=version_key)
    if is_unsupported(release, minor, newest):
        return "unsupported", ""
    stated = release.get("stated")
    if stated and in_range(minor, stated, newest):
        return "stated", ""
    return "unknown", ""
