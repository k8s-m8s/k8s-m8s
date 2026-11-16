"""Unit tests for the logic that decides what every cell shows."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from common import (  # noqa: E402
    cell,
    dump_releases,
    format_range,
    load_yaml,
    ordered,
    release_key,
    release_label,
    tool_files,
)
from validate import bands_overlap, date_problem, is_under  # noqa: E402
from verify import reconcile  # noqa: E402

WINDOW = ["1.37", "1.36", "1.35"]


class CellTest(unittest.TestCase):
    def test_tested_patch_and_minor(self):
        release = {"tested": ["1.36.5", "1.37"]}
        self.assertEqual(cell(release, "1.36", WINDOW), ("tested", "1.36.5"))
        self.assertEqual(cell(release, "1.37", WINDOW), ("tested", "1.37"))
        self.assertEqual(cell(release, "1.35", WINDOW), ("unknown", ""))

    def test_several_tested_patches_of_one_minor(self):
        release = {"tested": ["1.36.7", "1.36.5"]}
        self.assertEqual(cell(release, "1.36", WINDOW), ("tested", "1.36.5, 1.36.7"))

    def test_stated_is_never_reported_as_tested(self):
        release = {"stated": {"min": "1.18", "max": "latest", "wording": "x"}}
        for minor in WINDOW:
            self.assertEqual(cell(release, minor, WINDOW), ("stated", ""))

    def test_stated_range_is_inclusive_and_bounded(self):
        release = {"stated": {"min": "1.30", "max": "1.35", "wording": "x"}}
        self.assertEqual(cell(release, "1.35", WINDOW)[0], "stated")
        self.assertEqual(cell(release, "1.36", WINDOW)[0], "unknown")

    def test_stated_without_lower_bound(self):
        release = {"stated": {"max": "1.35", "wording": "x"}}
        self.assertEqual(cell(release, "1.35", WINDOW)[0], "stated")
        self.assertEqual(cell(release, "1.36", WINDOW)[0], "unknown")

    def test_tested_beats_stated(self):
        release = {"tested": ["1.37.1"], "stated": {"min": "1.18", "max": "latest", "wording": "x"}}
        self.assertEqual(cell(release, "1.37", WINDOW)[0], "tested")
        self.assertEqual(cell(release, "1.36", WINDOW)[0], "stated")

    def test_unsupported_minor_and_ranges(self):
        release = {
            "stated": {"min": "1.19", "max": "1.35", "wording": "x"},
            "unsupported": ["1.35", {"max": "1.18"}, {"min": "1.37", "max": "latest"}],
        }
        self.assertEqual(cell(release, "1.35", WINDOW)[0], "unsupported")
        self.assertEqual(cell(release, "1.37", WINDOW)[0], "unsupported")
        self.assertEqual(cell(release, "1.36", WINDOW)[0], "unknown")

    def test_silence_is_unknown(self):
        self.assertEqual(cell({}, "1.37", WINDOW), ("unknown", ""))


class LabelTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(release_label({"version": "1.19"}), "1.19")
        self.assertEqual(release_label({"version_range": {"min": "0.18"}}), "0.18 and later")
        self.assertEqual(release_label({"version_range": {"max": "0.9"}}), "0.9 and earlier")
        self.assertEqual(release_label({"version_range": {"min": "0.10", "max": "0.17"}}), "0.10 to 0.17")

    def test_ranges(self):
        self.assertEqual(format_range({"min": "1.18", "max": "latest"}), "1.18 to latest")
        self.assertEqual(format_range({"max": "1.20"}), "up to 1.20")
        self.assertEqual(format_range({"min": "1.31", "max": "1.31"}), "1.31")

    def test_sort_order_is_numeric_and_open_bands_come_last(self):
        releases = [{"version": "2.9"}, {"version_range": {"min": "2.11"}}, {"version": "2.10"}]
        ordered = [release_label(r) for r in sorted(releases, key=release_key, reverse=True)]
        self.assertEqual(ordered, ["2.11 and later", "2.10", "2.9"])


class ValidationTest(unittest.TestCase):
    def test_bands_overlap(self):
        version = lambda v: {"version": v}  # noqa: E731
        band = lambda **bounds: {"version_range": bounds}  # noqa: E731
        self.assertTrue(bands_overlap(band(min="0.18"), version("0.19")))
        self.assertFalse(bands_overlap(band(min="0.18"), version("0.17")))
        self.assertTrue(bands_overlap(band(min="0.10", max="0.17"), band(min="0.17")))
        self.assertFalse(bands_overlap(band(min="0.10", max="0.17"), band(min="0.18")))
        self.assertTrue(bands_overlap(band(max="0.9"), version("0.9.1")))

    def test_dates_must_be_real_and_not_in_the_future(self):
        self.assertIsNone(date_problem(None))
        self.assertIsNone(date_problem("2026-11-15"))
        self.assertIn("not a real date", date_problem("2026-02-30"))
        self.assertIn("future", date_problem("2999-01-01"))

    def test_source_must_sit_under_the_repo(self):
        repo = "https://github.com/velero-io/velero"
        self.assertTrue(is_under(repo + "#velero-compatibility-matrix", repo))
        self.assertTrue(is_under(repo + "/blob/main/README.md", repo))
        self.assertFalse(is_under("https://github.com/velero-io/velero-fork", repo))
        self.assertFalse(is_under("https://example.com/velero", repo))


class DataFileLayoutTest(unittest.TestCase):
    def test_every_data_file_is_in_the_layout_the_writer_produces(self):
        # verify.py rewrites these files, so a rewrite of unchanged data must change nothing
        for path in tool_files():
            _, _, body = path.read_text(encoding="utf-8").partition("\nreleases:\n")
            self.assertEqual(dump_releases(ordered(load_yaml(path)["releases"])), body, path.name)

    def test_wording_is_quoted_only_when_yaml_needs_it(self):
        release = {"version": "1.0", "stated": {"max": "latest", "wording": "x"}, "source": "https://e.org", "verified": "2026-01-01"}
        self.assertIn("wording: x\n", dump_releases([release]))
        release["stated"]["wording"] = "supported: yes"
        self.assertIn('wording: "supported: yes"\n', dump_releases([release]))


class ReconcileTest(unittest.TestCase):
    TODAY = "2026-12-01"

    def recorded(self):
        return [
            {"version": "1.2", "tested": ["1.37"], "source": "https://e.org/a", "source_commit": "aaaaaaa", "verified": "2026-11-01"},
            {"version": "1.1", "tested": ["1.36"], "source": "https://e.org/a", "source_commit": "aaaaaaa", "verified": "2026-11-01"},
            {"version": "1.0", "tested": ["1.35"], "source": "https://e.org/a", "source_commit": "aaaaaaa", "verified": "2026-11-01"},
        ]

    def upstream(self):
        row = {"source": "https://e.org/a", "source_commit": "bbbbbbb"}
        return [
            {"version": "1.3", "tested": ["1.38", "1.37"], **row},  # new
            {"version": "1.2", "tested": ["1.37"], **row},  # unchanged
            {"version": "1.1", "tested": ["1.36", "1.37"], **row},  # changed
        ]  # 1.0 removed

    def test_report_changes_nothing(self):
        releases = self.recorded()
        matching, differences, problems = reconcile(releases, self.upstream(), self.TODAY, "report")
        self.assertEqual((matching, len(differences), problems), (1, 3, []))
        self.assertEqual(releases, self.recorded())

    def test_write_refreshes_only_matching_entries(self):
        releases = self.recorded()
        reconcile(releases, self.upstream(), self.TODAY, "write")
        self.assertEqual((releases[0]["verified"], releases[0]["source_commit"]), (self.TODAY, "bbbbbbb"))
        self.assertEqual(releases[1:], self.recorded()[1:])

    def test_apply_takes_over_new_changed_and_removed_rows(self):
        releases = self.recorded()
        reconcile(releases, self.upstream(), self.TODAY, "apply")
        by_version = {r["version"]: r for r in releases}
        self.assertEqual(by_version["1.3"]["tested"], ["1.37", "1.38"])
        self.assertEqual(by_version["1.3"]["verified"], self.TODAY)
        self.assertEqual(by_version["1.1"]["tested"], ["1.36", "1.37"])
        self.assertEqual(by_version["1.1"]["source_commit"], "bbbbbbb")
        self.assertEqual(by_version["1.0"]["delisted"], self.TODAY)
        self.assertEqual(by_version["1.0"]["tested"], ["1.35"])  # last known values are kept
        self.assertEqual(by_version["1.0"]["verified"], "2026-11-01")

    def test_a_relisted_row_is_left_for_a_human(self):
        releases = [{**self.recorded()[0], "delisted": "2026-11-10"}]
        _, differences, problems = reconcile(releases, self.upstream()[1:2], self.TODAY, "apply")
        self.assertEqual(differences, [])
        self.assertEqual(len(problems), 1)
        self.assertEqual(releases[0]["delisted"], "2026-11-10")


if __name__ == "__main__":
    unittest.main()
