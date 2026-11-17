"""Fixtures shared by the automation tests: a small tool file, and a throwaway git repository."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common import dump_releases, ordered, release_label  # noqa: E402
from verify import finding, snapshot  # noqa: E402

REPO = "https://github.com/example/demo"
SHA_A, SHA_B = "a" * 40, "b" * 40
HEADER = f"name: demo\ntitle: Demo\ncategory: networking\nrepo: {REPO}\nupstream_license: Apache-2.0\n"


def entry(version, tested, commit=SHA_A, verified="2026-11-01", **extra):
    return {"version": version, "tested": tested, "source": f"{REPO}/blob/main/README.md",
            "source_commit": commit, "verified": verified, **extra}


def releases(count=3):
    return [entry(f"1.{n}", [f"1.{30 + n}"]) for n in range(count, 0, -1)]


def tool_text(items, header=HEADER):
    return header + "releases:\n" + dump_releases(ordered(items))


def claim_findings(tool, old_items, new_items):
    """The claim findings verify.py would write for the entries that differ."""
    old = {release_label(r): r for r in old_items}
    result = []
    for r in new_items:
        before = old.get(release_label(r))
        if before != r:
            result.append(finding(tool, release_label(r), "claim", "changed", snapshot(before), snapshot(r)))
    return result


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path, files):
    """A git repository at path with one commit on main holding files. Returns its SHA."""
    git(path.parent, "init", "-q", "-b", "main", str(path))
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "test")
    return commit(path, files, "base")


def commit(path, files, message):
    for name, text in files.items():
        target = Path(path) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD")
