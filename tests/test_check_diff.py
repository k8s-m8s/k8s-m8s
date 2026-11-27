"""Unit tests for check_diff.py: the bookkeeping check, merge gate 4 and the window check."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_diff import changed_paths, check_bookkeeping, check_claim, check_window, merge_base, reader, rolled  # noqa: E402
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


def changed(fn, base=None, removed_commit=SHA_B):
    """The demo releases with fn applied to a copy, and the matching findings.
    A removal's finding names removed_commit, as verify.py's does."""
    base = base or releases()
    new = copy.deepcopy(base)
    fn(new)
    items = claim_findings("demo", base, new)
    for item in items:
        if "delisted" in (item["new"] or {}) and "delisted" not in (item["old"] or {}):
            item["removed_commit"] = removed_commit
    return tool_text(new), items


def delist(*indexes, date="2026-12-01"):
    def fn(r):
        for index in indexes:
            r[index]["delisted"] = date
    return fn


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

    def test_a_removal_naming_the_commit_where_the_row_is_gone_passes(self):
        text, items = changed(delist(0))
        self.assertEqual(items[0]["fields"], ["delisted"])
        self.assertEqual(check("claim", files(**{PATH: text}), items), [])

    def test_a_removal_without_that_commit_is_refused(self):
        for missing in (None, "abc1234"):
            text, items = changed(delist(0), removed_commit=missing)
            self.assertTrue(any("showing the removal" in p for p in check("claim", files(**{PATH: text}), items)), missing)

    def test_a_plain_removal_changes_nothing_else(self):
        text, items = changed(lambda r: r[0].update(delisted="2026-12-01", verified="2026-12-01"))
        self.assertTrue(any("beyond delisted" in p for p in check("claim", files(**{PATH: text}), items)))

    def test_a_row_read_from_history_may_take_its_last_listed_values(self):
        last = {"source": "https://github.com/example/demo/blob/" + SHA_B + "/README.md", "source_commit": SHA_B,
                "tested": ["1.32"], "verified": "2026-12-01", "delisted": "2026-11-20"}
        text, items = changed(lambda r: r[0].update(last))
        self.assertEqual(items[0]["fields"], ["tested", "source", "delisted"])
        self.assertEqual(check("claim", files(**{PATH: text}), items), [])

    def test_more_than_two_removals_are_refused(self):
        base = releases(4)
        text, items = changed(delist(0, 1, 2), base=base)
        problems = check("claim", files(**{PATH: text}), items, files(**{PATH: tool_text(base)}))
        self.assertTrue(any("3 entries delisted" in p for p in problems))

    def test_delisting_every_current_entry_is_refused(self):
        base = releases(2)
        text, items = changed(delist(0, 1), base=base)
        problems = check("claim", files(**{PATH: text}), items, files(**{PATH: tool_text(base)}))
        self.assertTrue(any("every entry" in p for p in problems))

    def test_history_never_changes(self):
        base = [*releases(), entry("1.0", ["1.30"], delisted="2026-10-01")]
        for fn in (lambda r: r[3].update(delisted="2026-10-02"), lambda r: r[3].pop("delisted"),
                   lambda r: r[3].update(tested=["1.29"])):
            text, items = changed(fn, base=base)
            problems = check("claim", files(**{PATH: text}), items, files(**{PATH: tool_text(base)}))
            self.assertTrue(any("already delisted" in p for p in problems))

    def test_an_entry_added_already_delisted_is_refused(self):
        text, items = changed(lambda r: r.append(entry("0.9", ["1.29"], delisted="2026-01-01")))
        self.assertTrue(any("added already delisted" in p for p in check("claim", files(**{PATH: text}), items)))

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


def window_roll(window=("1.38", "1.37", "1.36"), commit_sha="c" * 40, text=None):
    """data/kubernetes.yaml after a roll, and its finding, as verify.py writes them."""
    listed = ", ".join(f'"{m}"' for m in window)
    text = text or KUBE.replace('["1.37", "1.36", "1.35"]', f"[{listed}]").replace("a" * 40, commit_sha).replace(
        "2026-11-01", "2026-12-17")
    item = {"id": "kubernetes/window", "tool": "kubernetes", "version": "window", "class": "window", "fields": ["window"],
            "old": {"window": ["1.37", "1.36", "1.35"], "source_commit": "a" * 40},
            "new": {"window": list(window), "source_commit": commit_sha}}
    return text, [item]


class WindowGateTest(unittest.TestCase):
    def check(self, kube, items, **extra):
        base, head = files(), {**files(), "data/kubernetes.yaml": kube, **extra}
        paths = sorted(p for p in set(base) | set(head) if base.get(p) != head.get(p))
        return check_window(base.get, head.get, paths, items)

    def test_a_roll_by_one_minor_passes(self):
        self.assertEqual(self.check(*window_roll()), [])

    def test_anything_but_the_next_minor_is_refused(self):
        for window in (("1.39", "1.38", "1.37"), ("1.36", "1.35", "1.34"), ("1.38", "1.36", "1.37"),
                       ("1.38", "1.37"), ("1.38", "1.37", "1.36", "1.35"), ("1.38.0", "1.37", "1.36")):
            self.assertTrue(any("rolled forward" in p for p in self.check(*window_roll(window))), window)

    def test_only_the_window_file_may_change(self):
        kube, items = window_roll()
        self.assertTrue(self.check(kube, items, **{PATH: "x"}))
        other = kube.replace("https://kubernetes.io/releases/", "https://example.invalid/")
        self.assertTrue(any("outside window" in p for p in self.check(other, items)))
        self.assertTrue(any("outside window" in p for p in self.check(kube + "# note\n", items)))

    def test_a_short_commit_is_refused(self):
        self.assertTrue(any("full commit" in p for p in self.check(*window_roll(commit_sha="abc1234"))))

    def test_the_finding_must_match_the_diff(self):
        kube, items = window_roll()
        items[0]["new"]["window"] = ["1.38", "1.37", "1.35"]
        self.assertTrue(any("does not match" in p for p in self.check(kube, items)))
        self.assertTrue(any("one kubernetes/window finding" in p for p in self.check(kube, [])))
        self.assertTrue(any("one kubernetes/window finding" in p for p in self.check(kube, items * 2)))

    def test_rolled(self):
        self.assertEqual(rolled(["1.37", "1.36", "1.35"]), ["1.38", "1.37", "1.36"])
        self.assertEqual(rolled(["1.9", "1.8", "1.7"]), ["1.10", "1.9", "1.8"])
        self.assertIsNone(rolled(["1.37", "1.35", "1.34"]))
        self.assertIsNone(rolled("1.37"))


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
