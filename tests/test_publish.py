"""Unit tests for publish_findings.py's decisions, against a fake GitHub API."""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import findings as block  # noqa: E402
import publish_findings as publish  # noqa: E402
from helpers import releases, tool_text  # noqa: E402

BOT = {"login": "k8s-m8s-automation[bot]", "type": "Bot"}
ENV = {"GITHUB_REPOSITORY": "k8s-m8s/k8s-m8s", "AUTOMATION_LOGIN": BOT["login"]}


def needs_human(tool, reason="statement changed shape"):
    return {"id": f"{tool}/extractor", "tool": tool, "class": "needs-human", "reason": reason, "message": "m", "fields": []}


def claim(tool="velero", version="1.19", tested=("1.37.1",)):
    new = {"version": version, "tested": list(tested), "source": "https://github.com/velero-io/velero#x", "source_commit": "a" * 40}
    return {"id": f"{tool}/{version}", "tool": tool, "version": version, "class": "claim", "fields": ["tested"],
            "source": new["source"], "source_commit": new["source_commit"], "old": {**new, "tested": ["1.36.5"]},
            "new": new, "message": "changed"}


class IssueTest(unittest.TestCase):
    def run_issues(self, report, open_issues):
        calls = []
        with mock.patch.dict(os.environ, ENV), \
                mock.patch.object(publish.ghapi, "paged", return_value=open_issues), \
                mock.patch.object(publish.ghapi, "call", side_effect=lambda *a, **k: calls.append(a) or {"number": 99}), \
                redirect_stdout(io.StringIO()):
            publish.publish_issues(report)
        return calls

    def test_one_issue_per_tool_and_reason(self):
        report = {"checked": [], "findings": [needs_human("rook"), needs_human("rook")]}
        calls = self.run_issues(report, [])
        self.assertEqual([(c[0], c[2]["title"]) for c in calls], [("POST", "[needs-human] rook: statement changed shape")])

    def test_an_open_issue_is_not_duplicated(self):
        report = {"checked": [], "findings": [needs_human("rook")]}
        existing = [{"number": 3, "title": "[needs-human] rook: statement changed shape", "body": "old", "user": BOT}]
        calls = self.run_issues(report, existing)
        self.assertEqual([c[0] for c in calls], ["PATCH"])  # body refreshed, no new issue

    def test_an_issue_closes_once_the_tool_reads_cleanly(self):
        existing = [{"number": 3, "title": "[needs-human] rook: statement changed shape", "body": "b", "user": BOT}]
        calls = self.run_issues({"checked": ["rook"], "findings": []}, existing)
        self.assertEqual([c[0] for c in calls], ["POST", "PATCH"])
        self.assertEqual(calls[1][2]["state"], "closed")

    def test_issues_for_tools_not_run_stay_open(self):
        existing = [{"number": 3, "title": "[needs-human] rook: statement changed shape", "body": "b", "user": BOT}]
        self.assertEqual(self.run_issues({"checked": ["velero"], "findings": []}, existing), [])

    def test_issues_opened_by_people_are_never_closed(self):
        existing = [{"number": 3, "title": "[needs-human] rook: x", "body": "b", "user": {"login": "someone", "type": "User"}}]
        self.assertEqual(self.run_issues({"checked": ["rook"], "findings": []}, existing), [])


class ClaimPullTest(unittest.TestCase):
    def run_claims(self, items, existing_body=None, state="clean", problems=()):
        report = {"checked": ["velero"], "findings": items}
        pull = None if existing_body is None else {"number": 4, "body": existing_body, "title": "t", "user": BOT,
                                                    "head": {"ref": "automated/claim/velero", "repo": {"full_name": ENV["GITHUB_REPOSITORY"]}}}
        with mock.patch.dict(os.environ, ENV), \
                mock.patch.object(publish, "open_pull", return_value=pull), \
                mock.patch.object(publish.ghapi, "get", return_value={"mergeable_state": state}), \
                mock.patch.object(publish, "read_data", return_value="text"), \
                mock.patch.object(publish, "gate_problems", return_value=list(problems)), \
                mock.patch.object(publish, "upsert_pull") as upsert, \
                mock.patch.object(publish, "close_pull") as close, \
                redirect_stdout(io.StringIO()):
            publish.publish_claims(report, "unused", "b" * 40)
        return upsert, close

    def test_the_same_findings_keep_the_branch(self):
        items = [claim()]
        upsert, _ = self.run_claims(items, block.render(items))
        self.assertIs(upsert.call_args.args[4], False)  # rewrite

    def test_new_findings_rewrite_the_branch(self):
        upsert, _ = self.run_claims([claim(tested=("1.37.2",))], block.render([claim()]))
        self.assertIs(upsert.call_args.args[4], True)

    def test_a_conflicting_pull_request_is_rebuilt(self):
        items = [claim()]
        upsert, _ = self.run_claims(items, block.render(items), state="dirty")
        self.assertIs(upsert.call_args.args[4], True)

    def test_a_change_the_claim_gate_would_refuse_is_labelled_for_a_human(self):
        upsert, _ = self.run_claims([claim()], problems=["1.0: changes an entry that is already delisted"])
        self.assertIn("human-merge", upsert.call_args.args[3])
        self.assertIn("already delisted", upsert.call_args.args[2])

    def test_a_removal_the_gate_accepts_goes_to_the_review(self):
        item = claim()
        item.update(fields=["delisted"], new={**item["old"], "delisted": "2026-12-01"}, removed_commit="e" * 40)
        upsert, _ = self.run_claims([item])
        self.assertEqual(upsert.call_args.args[3], ["claim-change"])
        self.assertIn("gone [at eeeeeee]", upsert.call_args.args[2])

    def test_gate_problems_reads_main_and_the_new_file(self):
        with mock.patch.object(publish, "read_data", return_value="new text"):
            seen = publish.gate_problems(lambda read_base, read_head, paths, items: [read_head(paths[0]), paths],
                                         "data/tools/velero.yaml", "unused", [])
        self.assertEqual(seen, ["new text", ["data/tools/velero.yaml"]])

    def test_a_tool_that_matches_again_closes_its_pull_request(self):
        upsert, close = self.run_claims([], block.render([claim()]))
        upsert.assert_not_called()
        close.assert_called_once()

    def run_window(self, problems=()):
        item = {"id": "kubernetes/window", "tool": "kubernetes", "version": "window", "class": "window",
                "fields": ["window"], "source": "https://kubernetes.io/releases/", "source_commit": "c" * 40,
                "old": {"window": ["1.37", "1.36", "1.35"], "source_commit": "a" * 40},
                "new": {"window": ["1.38", "1.37", "1.36"], "source_commit": "c" * 40}, "message": "m"}
        with mock.patch.dict(os.environ, ENV), \
                mock.patch.object(publish, "open_pull", return_value=None), \
                mock.patch.object(publish, "read_data", return_value="text"), \
                mock.patch.object(publish, "gate_problems", return_value=list(problems)), \
                mock.patch.object(publish, "upsert_pull") as upsert, \
                redirect_stdout(io.StringIO()):
            publish.publish_window({"checked": ["kubernetes"], "findings": [item]}, "unused", "b" * 40)
        return upsert

    def test_a_window_roll_is_not_labelled_for_a_human(self):
        upsert = self.run_window()
        self.assertEqual(upsert.call_args.args[3], ["window-change"])
        self.assertIn("merges automatically", upsert.call_args.args[2])

    def test_a_window_change_the_gate_would_refuse_is_labelled(self):
        upsert = self.run_window(problems=["window is not rolled forward by one minor"])
        self.assertEqual(upsert.call_args.args[3], ["window-change", "human-merge"])

    def test_only_data_files_are_committed(self):
        with self.assertRaises(SystemExit):
            publish.commit_files("b" * 40, {".github/workflows/x.yml": "x"}, "m")


class BookkeepingPullTest(unittest.TestCase):
    TOOLS = ("rook", "velero")

    def run_bookkeeping(self, findings=(), open_pulls=()):
        """Main has every tool verified 2026-11-01; the refreshed copy has 2026-12-01."""
        refreshed = [{**r, "verified": "2026-12-01"} for r in releases()]
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "root", Path(tmp) / "out"
            for base, items in ((root, releases()), (out, refreshed)):
                for tool in self.TOOLS:
                    path = base / "data" / "tools" / f"{tool}.yaml"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(tool_text(items).replace("demo", tool), encoding="utf-8")
            report = {"date": "2026-12-01", "findings": list(findings)}
            with mock.patch.dict(os.environ, ENV), mock.patch.object(publish, "ROOT", root), \
                    mock.patch.object(publish.ghapi, "paged", return_value=list(open_pulls)), \
                    mock.patch.object(publish, "upsert_pull") as upsert, \
                    redirect_stdout(io.StringIO()):
                publish.publish_bookkeeping(report, str(out), "b" * 40)
        return upsert

    def files(self, upsert):
        return sorted(upsert.call_args.args[6])

    def test_every_refreshed_tool_is_in_the_pull_request(self):
        upsert = self.run_bookkeeping()
        self.assertEqual(self.files(upsert), ["data/tools/rook.yaml", "data/tools/velero.yaml"])
        self.assertIn("Opened by `detect.yml`", upsert.call_args.args[2])

    def test_tools_with_a_change_or_problem_waiting_are_left_out(self):
        upsert = self.run_bookkeeping(findings=[claim("velero")])
        self.assertEqual(self.files(upsert), ["data/tools/rook.yaml"])
        self.assertIn("Left out until their change or problem is resolved: velero", upsert.call_args.args[2])
        upsert = self.run_bookkeeping(findings=[needs_human("velero")])
        self.assertEqual(self.files(upsert), ["data/tools/rook.yaml"])

    def test_tools_with_an_open_claim_pull_request_are_left_out(self):
        pulls = [{"head": {"ref": "automated/claim/rook"}}, {"head": {"ref": "automated/bookkeeping"}}]
        self.assertEqual(self.files(self.run_bookkeeping(open_pulls=pulls)), ["data/tools/velero.yaml"])

    def test_nothing_left_to_refresh_opens_nothing(self):
        self.run_bookkeeping(findings=[claim("velero"), claim("rook")]).assert_not_called()


if __name__ == "__main__":
    unittest.main()
