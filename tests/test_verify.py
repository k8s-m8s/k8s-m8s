"""Unit tests for verify.py's finding classes and the Kubernetes window roll."""

import datetime
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify  # noqa: E402
from helpers import releases as demo_releases, tool_text  # noqa: E402
from common import KUBERNETES_FILE, cell, load_yaml, tool_files  # noqa: E402
from extractors import UpstreamChanged, window_from_schedule  # noqa: E402
from generate import tool_page  # noqa: E402

TODAY = "2026-12-01"
ROW = {"source": "https://e.org/a", "source_commit": "a" * 40}


def recorded():
    return [
        {"version": "1.3", "tested": ["1.37"], **ROW, "verified": "2026-11-01"},
        {"version": "1.2", "tested": ["1.36"], **ROW, "verified": "2026-11-01"},
        {"version": "1.1", "tested": ["1.35"], **ROW, "verified": "2026-11-01"},
        {"version": "1.0", "tested": ["1.34"], **ROW, "verified": "2026-11-01", "delisted": "2026-10-01"},
    ]


def classes(findings):
    return {f["id"]: (f["class"], tuple(f["fields"])) for f in findings}


class ClassTest(unittest.TestCase):
    def run_reconcile(self, upstream, mode="report"):
        releases, findings = recorded(), []
        verify.reconcile(releases, upstream, TODAY, mode, "demo", findings)
        return releases, findings

    def test_unchanged_entries_with_the_same_commit_are_not_findings(self):
        upstream = [{k: v for k, v in r.items() if k != "verified"} for r in recorded()[:3]]
        _, findings = self.run_reconcile(upstream)
        self.assertEqual(findings, [])

    def test_a_moved_source_commit_alone_is_bookkeeping(self):
        upstream = [{**r, "source_commit": "b" * 40} for r in recorded()[:3]]
        for r in upstream:
            r.pop("verified")
        _, findings = self.run_reconcile(upstream)
        self.assertEqual(set(classes(findings).values()), {("bookkeeping", ("source_commit",))})
        self.assertEqual(len(findings), 3)

    def test_claim_changes_additions_and_removals_are_claims(self):
        upstream = [
            {"version": "1.4", "tested": ["1.38"], **ROW},               # added
            {"version": "1.3", "tested": ["1.37", "1.38"], **ROW},       # tested changed
            {"version": "1.2", "stated": {"min": "1.30", "max": "latest", "wording": "w"}, **ROW},
        ]                                                                # 1.1 removed
        _, findings = self.run_reconcile(upstream)
        self.assertEqual(classes(findings), {
            "demo/1.4": ("claim", ("tested", "source")),
            "demo/1.3": ("claim", ("tested",)),
            "demo/1.2": ("claim", ("tested", "stated")),
            "demo/1.1": ("claim", ("delisted",)),
        })
        added = next(f for f in findings if f["id"] == "demo/1.4")
        self.assertIsNone(added["old"])
        self.assertEqual(added["new"]["source_commit"], "a" * 40)
        self.assertNotIn("removed_commit", added)
        removal = next(f for f in findings if f["id"] == "demo/1.1")
        self.assertEqual(removal["removed_commit"], "a" * 40)  # today's reading of the same source, without the row

    def test_a_removal_with_no_reading_of_its_source_names_no_commit(self):
        upstream = [{**r, "source": "https://e.org/other"} for r in recorded()[:2]]
        for r in upstream:
            r.pop("verified")
        _, findings = self.run_reconcile(upstream)
        removal = next(f for f in findings if f["id"] == "demo/1.1")
        self.assertIn("removed_commit", removal)
        self.assertIsNone(removal["removed_commit"])

    def test_a_row_upstream_history_delists_names_the_commit_that_replaced_it(self):
        history = {"version": "1.1", "tested": ["1.35"], "source": "https://e.org/a/old", "source_commit": "c" * 40,
                   "delisted": "2026-11-20", "removed_commit": "d" * 40}
        upstream = [{k: v for k, v in r.items() if k != "verified"} for r in recorded()[:2]] + [history]
        releases, findings = self.run_reconcile(upstream, "claims")
        removal = next(f for f in findings if f["id"] == "demo/1.1")
        self.assertEqual((removal["removed_commit"], removal["new"]["delisted"]), ("d" * 40, "2026-11-20"))
        self.assertNotIn("removed_commit", removal["new"])
        self.assertFalse(any("removed_commit" in r for r in releases))  # evidence for the review, never data

    def test_a_relisted_row_needs_a_human(self):
        upstream = [{k: v for k, v in r.items() if k not in ("verified", "delisted")} for r in recorded()]
        _, findings = self.run_reconcile(upstream)
        self.assertEqual(classes(findings), {"demo/1.0": ("needs-human", ("delisted",))})
        self.assertEqual(findings[0]["reason"], "delisted row listed again")

    def test_claims_mode_leaves_matching_entries_alone(self):
        upstream = [{**r, "source_commit": "b" * 40} for r in recorded()[:3]]
        for r in upstream:
            r.pop("verified")
        upstream[0]["tested"] = ["1.37", "1.38"]
        releases, _ = self.run_reconcile(upstream, "claims")
        self.assertEqual((releases[0]["verified"], releases[0]["source_commit"]), (TODAY, "b" * 40))
        self.assertEqual(releases[1:], recorded()[1:])  # untouched, verified included

    def test_findings_are_stable_between_days(self):
        upstream = [{"version": "1.3", "tested": ["1.38"], **ROW}, *[{k: v for k, v in r.items() if k != "verified"} for r in recorded()[1:3]]]
        first, second = [], []
        verify.reconcile(recorded(), upstream, "2026-12-01", "claims", "demo", first)
        verify.reconcile(recorded(), upstream, "2026-12-02", "claims", "demo", second)
        self.assertEqual(first, second)

    def test_unreadable_statements_need_a_human(self):
        def broken():
            raise UpstreamChanged("table header changed")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(verify.EXTRACTORS, {"velero": broken}):
            report = Path(tmp) / "report.json"
            with mock.patch.object(sys, "argv", ["verify.py", "--detect-only", "--tools", "velero", "--report-json", str(report)]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(verify.main(), 0)
            data = json.loads(report.read_text())
        self.assertEqual(data["checked"], [])
        self.assertEqual([(f["id"], f["class"], f["reason"]) for f in data["findings"]],
                         [("velero/extractor", "needs-human", "statement changed shape")])


SCHEDULE_138 = """
schedules:
- release: "1.38"
  releaseDate: "2026-12-16"
- release: "1.37"
  releaseDate: "2026-08-26"
- release: "1.36"
  releaseDate: "2026-04-22"
- release: "1.35"
  releaseDate: "2025-12-17"
"""


class SingleReadTest(unittest.TestCase):
    """--claims-only --bookkeeping-out: one reading of upstream, written two ways."""

    KUBE = 'window: ["1.37", "1.36", "1.35"]\nsource: https://kubernetes.io/releases/\nsource_commit: ' + "a" * 40 + "\nverified: 2026-11-01\n"

    def run_main(self, tmp, *args):
        data = Path(tmp) / "data"
        (data / "tools").mkdir(parents=True)
        tool, kube = data / "tools" / "demo.yaml", data / "kubernetes.yaml"
        tool.write_text(tool_text(demo_releases()), encoding="utf-8")
        kube.write_text(self.KUBE, encoding="utf-8")
        upstream = [{k: v for k, v in r.items() if k != "verified"} for r in demo_releases()]
        upstream[0]["tested"] = ["1.33", "1.34"]  # 1.3 changed upstream; 1.2 and 1.1 still match
        reads = {"demo": 0, "window": 0}

        def extractor():
            reads["demo"] += 1
            return [dict(r) for r in upstream]

        def window():
            reads["window"] += 1
            return ["1.37", "1.36", "1.35"], "b" * 40  # same window, file edited upstream

        report = Path(tmp) / "report.json"
        argv = ["verify.py", *args, "--report-json", str(report)]
        with mock.patch.dict(verify.EXTRACTORS, {"demo": extractor}, clear=True), \
                mock.patch.object(verify, "tool_files", return_value=[tool]), \
                mock.patch.object(verify, "KUBERNETES_FILE", kube), \
                mock.patch.object(verify, "kubernetes_window", side_effect=window), \
                mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(), 0)
        return reads, tool, kube, json.loads(report.read_text())

    def test_one_reading_writes_the_claims_and_the_bookkeeping_refresh(self):
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            reads, tool, kube, report = self.run_main(tmp, "--claims-only", "--bookkeeping-out", str(out))
            self.assertEqual(reads, {"demo": 1, "window": 1})
            claims = {r["version"]: r for r in load_yaml(tool)["releases"]}
            refreshed = {r["version"]: r for r in load_yaml(out / "data" / "tools" / "demo.yaml")["releases"]}
            self.assertEqual((claims["1.3"]["tested"], claims["1.3"]["verified"]), (["1.33", "1.34"], today))
            self.assertEqual(claims["1.2"]["verified"], "2026-11-01")         # claims leave matching entries alone
            self.assertEqual((refreshed["1.2"]["verified"], refreshed["1.1"]["verified"]), (today, today))
            self.assertEqual((refreshed["1.3"]["tested"], refreshed["1.3"]["verified"]), (["1.33"], "2026-11-01"))
            self.assertEqual(kube.read_text(), self.KUBE)                    # a matching window is left alone ...
            bookkept = load_yaml(out / "data" / "kubernetes.yaml")            # ... and refreshed in the copy
            self.assertEqual((bookkept["source_commit"], bookkept["verified"]), ("b" * 40, today))
            self.assertEqual(sorted((f["id"], f["class"]) for f in report["findings"]),
                             [("demo/1.3", "claim"), ("kubernetes/window", "bookkeeping")])

    def test_the_bookkeeping_copy_needs_claims_mode(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.run_main(tmp, "--write", "--bookkeeping-out", str(Path(tmp) / "out"))


class WindowRollTest(unittest.TestCase):
    def test_a_new_minor_rolls_the_window(self):
        window = window_from_schedule(SCHEDULE_138, today="2026-12-16")
        self.assertEqual(window, ["1.38", "1.37", "1.36"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kubernetes.yaml"
            shutil.copyfile(KUBERNETES_FILE, path)
            findings = []
            differences = verify.verify_window("claims", TODAY, findings, fetch=lambda: (window, "c" * 40), path=path)
            self.assertEqual(len(differences), 1)
            self.assertEqual([f["class"] for f in findings], ["window"])
            data = load_yaml(path)
            self.assertEqual(data["window"], ["1.38", "1.37", "1.36"])
            self.assertEqual((data["source_commit"], data["verified"]), ("c" * 40, TODAY))

    def test_a_scheduled_minor_joins_only_on_its_release_date(self):
        self.assertEqual(window_from_schedule(SCHEDULE_138, today="2026-12-15"), ["1.37", "1.36", "1.35"])
        with self.assertRaises(UpstreamChanged):
            window_from_schedule(SCHEDULE_138.replace('  releaseDate: "2026-12-16"\n', ""), today="2026-12-16")

    def test_a_matching_window_is_left_alone_in_claims_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kubernetes.yaml"
            shutil.copyfile(KUBERNETES_FILE, path)
            before = path.read_text()
            recorded_window = load_yaml(path)["window"]
            verify.verify_window("claims", TODAY, [], fetch=lambda: (recorded_window, "c" * 40), path=path)
            self.assertEqual(path.read_text(), before)

    def test_the_new_column_starts_unknown_unless_an_open_range_covers_it(self):
        window = ["1.38", "1.37", "1.36"]
        self.assertEqual(cell({"stated": {"min": "1.18", "max": "latest", "wording": "x"}}, "1.38", window)[0], "stated")
        self.assertEqual(cell({"stated": {"min": "1.33", "max": "1.37", "wording": "x"}}, "1.38", window)[0], "unknown")
        self.assertEqual(cell({"tested": ["1.37.1"]}, "1.38", window)[0], "unknown")

    def test_data_for_the_dropped_minor_stays_on_history_pages(self):
        window = ["1.38", "1.37", "1.36"]
        found = False
        for path in tool_files():
            tool = load_yaml(path)
            on_135 = [r for r in tool["releases"] if any(v.startswith("1.35") for v in r.get("tested", []))]
            if on_135:
                page = tool_page(tool, window)
                for release in on_135:
                    for version in release["tested"]:
                        self.assertIn(version, page, f"{tool['name']} lost {version}")
                found = True
        self.assertTrue(found, "no recorded entry is tested on 1.35")


if __name__ == "__main__":
    unittest.main()
