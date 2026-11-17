"""Unit tests for publish_findings.py's decisions, against a fake GitHub API."""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import findings as block  # noqa: E402
import publish_findings as publish  # noqa: E402

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
    def run_claims(self, items, existing_body=None, state="clean"):
        report = {"checked": ["velero"], "findings": items}
        pull = None if existing_body is None else {"number": 4, "body": existing_body, "title": "t", "user": BOT,
                                                    "head": {"ref": "automated/claim/velero", "repo": {"full_name": ENV["GITHUB_REPOSITORY"]}}}
        with mock.patch.dict(os.environ, ENV), \
                mock.patch.object(publish, "open_pull", return_value=pull), \
                mock.patch.object(publish.ghapi, "get", return_value={"mergeable_state": state}), \
                mock.patch.object(publish, "read_data", return_value="text"), \
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

    def test_delisted_changes_are_labelled_for_a_human(self):
        item = claim()
        item["fields"] = ["delisted"]
        upsert, _ = self.run_claims([item])
        self.assertIn("human-merge", upsert.call_args.args[3])

    def test_a_tool_that_matches_again_closes_its_pull_request(self):
        upsert, close = self.run_claims([], block.render([claim()]))
        upsert.assert_not_called()
        close.assert_called_once()

    def test_only_data_files_are_committed(self):
        with self.assertRaises(SystemExit):
            publish.commit_files("b" * 40, {".github/workflows/x.yml": "x"}, "m")


if __name__ == "__main__":
    unittest.main()
