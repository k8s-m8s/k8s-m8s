"""Unit tests for check_diff.py: the bookkeeping check and merge gate 4."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_diff import changed_paths, check_bookkeeping, check_claim, merge_base, reader  # noqa: E402
from helpers import HEADER, SHA_B, claim_findings, commit, entry, make_repo, releases, tool_text  # noqa: E402

PATH = "data/tools/demo.yaml"
KUBE = 'window: ["1.37", "1.36", "1.35"]\nsource: https://kubernetes.io/releases/\nsource_commit: ' + "a" * 40 + "\nverified: 2026-11-01\n"


def files(**overrides):
    return {PATH: tool_text(releases()), "data/kubernetes.yaml": KUBE, **overrides}


def check(kind, head_files, items=None, base_files=None):
    base_files = base_files or files()
    paths = sorted(p for p in set(base_files) | set(head_files) if base_files.get(p) != head_files.get(p))
    read_base, read_head = base_files.get, head_files.get
    if kind == "bookkeeping":
        return check_bookkeeping(read_base, read_head, paths)
    return check_claim(read_base, read_head, paths, items if items is not None else [])


def changed(fn):
    """The demo releases with fn applied to a copy, and the matching findings."""
    new = copy.deepcopy(releases())
    fn(new)
    return tool_text(new), claim_findings("demo", releases(), new)


class BookkeepingTest(unittest.TestCase):
    def test_refreshed_dates_and_commits_pass(self):
        new = [{**r, "verified": "2026-12-01", "source_commit": SHA_B} for r in releases()]
        kube = KUBE.replace("2026-11-01", "2026-12-01")
        self.assertEqual(check("bookkeeping", files(**{PATH: tool_text(new), "data/kubernetes.yaml": kube})), [])

    def test_a_claim_change_fails(self):
        text, _ = changed(lambda r: r[0]["tested"].append("1.38"))
        self.assertTrue(check("bookkeeping", files(**{PATH: text})))

    def test_a_window_change_fails(self):
        kube = KUBE.replace('"1.35"', '"1.38"')
        self.assertTrue(check("bookkeeping", files(**{"data/kubernetes.yaml": kube})))

    def test_any_other_file_fails(self):
        for path in ("scripts/verify.py", ".github/workflows/x.yml", "requirements.lock", "README.md"):
            self.assertTrue(check("bookkeeping", files(**{path: "x"})), path)

    def test_a_header_change_fails(self):
        text = tool_text(releases(), HEADER.replace("Demo", "Demo2"))
        self.assertTrue(check("bookkeeping", files(**{PATH: text})))

    def test_a_comment_hidden_in_releases_fails(self):
        text = tool_text(releases()).replace("    verified: 2026-11-01\n", "    verified: 2026-11-01  # note\n", 1)
        self.assertTrue(check("bookkeeping", files(**{PATH: text})))


class ClaimGateTest(unittest.TestCase):
    def test_a_matching_claim_change_passes(self):
        text, items = changed(lambda r: r[0].update(tested=["1.33", "1.34"], verified="2026-12-01"))
        self.assertEqual(check("claim", files(**{PATH: text}), items), [])

    def test_an_added_entry_passes(self):
        text, items = changed(lambda r: r.append(entry("1.4", ["1.37"], verified="2026-12-01")))
        self.assertEqual(check("claim", files(**{PATH: text}), items), [])

    def test_two_tools_are_refused(self):
        text, items = changed(lambda r: r[0]["tested"].append("1.38"))
        other = {"data/tools/other.yaml": tool_text(releases()).replace("name: demo", "name: other")}
        problems = check("claim", {**files(**{PATH: text}), **other}, items, {**files(), **other, "data/tools/other.yaml": "x"})
        self.assertTrue(any("exactly one tool" in p for p in problems))

    def test_delisted_is_refused(self):
        text, items = changed(lambda r: r[0].update(delisted="2026-12-01"))
        self.assertTrue(any("delisted" in p for p in check("claim", files(**{PATH: text}), items)))

    def test_a_removed_entry_is_refused(self):
        text, items = changed(lambda r: r.pop())
        self.assertTrue(any("removed" in p for p in check("claim", files(**{PATH: text}), items)))

    def test_more_than_ten_entries_are_refused(self):
        text, items = changed(lambda r: r.extend(entry(f"2.{n}", ["1.37"]) for n in range(11)))
        self.assertTrue(any("limit" in p for p in check("claim", files(**{PATH: text}), items)))

    def test_a_diff_that_disagrees_with_its_findings_is_refused(self):
        text, items = changed(lambda r: r[0]["tested"].append("1.38"))
        items[0]["new"]["tested"] = ["1.33"]
        self.assertTrue(any("does not match" in p for p in check("claim", files(**{PATH: text}), items)))
        self.assertTrue(check("claim", files(**{PATH: text}), []))

    def test_protected_paths_are_refused(self):
        text, items = changed(lambda r: r[0]["tested"].append("1.38"))
        for path in (".github/workflows/review.yml", "scripts/verify.py", "schema/tool.schema.json",
                     "docs/maintainers/claude-review.md", ".claude/settings.json", "CLAUDE.md",
                     "requirements.txt", "requirements.lock", "zensical.toml"):
            problems = check("claim", files(**{PATH: text, path: "x"}), items)
            self.assertTrue(any("outside data/tools" in p for p in problems), path)

    def test_a_new_official_source_is_refused(self):
        header = HEADER + "official_sources:\n  - https://evil.example\n"
        text = tool_text([{**releases()[0], "source": "https://evil.example/x"}, *releases()[1:]], header)
        items = claim_findings("demo", releases(), [{**releases()[0], "source": "https://evil.example/x"}, *releases()[1:]])
        problems = check("claim", files(**{PATH: text}), items)
        self.assertTrue(any("header" in p for p in problems))
        self.assertTrue(any("not under" in p for p in problems))

    def test_a_short_source_commit_is_refused(self):
        text, items = changed(lambda r: r[0].update(source_commit="abc1234", tested=["1.38"]))
        self.assertTrue(any("source_commit" in p for p in check("claim", files(**{PATH: text}), items)))


class GitTest(unittest.TestCase):
    def test_reads_the_diff_from_the_merge_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            base = make_repo(repo, files())
            text, items = changed(lambda r: r[0].update(tested=["1.38"], verified="2026-12-01"))
            from helpers import git
            git(repo, "checkout", "-q", "-b", "automated/claim/demo")
            head = commit(repo, {PATH: text}, "claim")
            git(repo, "checkout", "-q", "main")
            commit(repo, {"README.md": "main moved on"}, "unrelated")  # not part of the pull request's diff
            mb = merge_base(repo, "main", head)
            self.assertEqual(mb, base)
            paths = changed_paths(repo, "main", head)
            self.assertEqual(paths, [PATH])
            self.assertEqual(check_claim(reader(repo, mb), reader(repo, head), paths, items), [])


if __name__ == "__main__":
    unittest.main()
