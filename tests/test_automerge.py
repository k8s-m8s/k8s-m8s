"""Unit tests for automerge.py's gates, against a fake GitHub API."""

import datetime
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import automerge  # noqa: E402
import findings as block  # noqa: E402
import reviews  # noqa: E402

REPO = "k8s-m8s/k8s-m8s"
AUTOMATION, REVIEWER = "k8s-m8s-automation[bot]", "k8s-m8s-reviewer[bot]"
HEAD, OLD = "c" * 40, "d" * 40
NOW = datetime.datetime(2026, 12, 1, 12, 0, tzinfo=datetime.timezone.utc)
ENV = {"GITHUB_REPOSITORY": REPO, "AUTOMATION_LOGIN": AUTOMATION, "REVIEWER_LOGIN": REVIEWER,
       "AUTOMERGE_ENABLED": "true", "AUTOMERGE_DRY_RUN": "false"}
ITEMS = [{"id": "demo/1.3", "tool": "demo", "class": "claim"}]
WINDOW_ITEMS = [{"id": "kubernetes/window", "tool": "kubernetes", "class": "window"}]
ROLLED = 'window: ["1.38", "1.37", "1.36"]\nsource: https://kubernetes.io/releases/\nsource_commit: ' + "e" * 40 + "\n"
RELEASED = {"html_url": "https://example.invalid/v1.38.0", "draft": False, "prerelease": False}


def stamp(hours_ago):
    return (NOW - datetime.timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def pull(ref="automated/claim/demo", **changes):
    items = WINDOW_ITEMS if ref == "automated/window" else ITEMS
    base = {"number": 5, "state": "open", "body": block.render(items), "labels": [], "created_at": stamp(3),
            "title": "demo: upstream claim change (1 entry)",
            "head": {"sha": HEAD, "ref": ref, "repo": {"full_name": REPO}}, "base": {"sha": "b" * 40},
            "user": {"login": AUTOMATION, "type": "Bot"}}
    base.update(changes)
    return base


def review(head=HEAD, verdict="confirmed", rows=None, login=REVIEWER, kind="Bot", hours_ago=1, edited=False, cid=10):
    rows = rows if rows is not None else [{"id": "demo/1.3", "verdict": "confirmed", "reason": "ok"}]
    problems = [] if verdict == "confirmed" else ["rejected"]
    created = stamp(hours_ago)
    return {"id": cid, "created_at": created, "updated_at": stamp(hours_ago - 0.5) if edited else created,
            "html_url": "https://example.invalid/c", "user": {"login": login, "type": kind},
            "body": reviews.render(head, rows, problems, "u")}


def check(name="validate", status="completed", conclusion="success", cid=1, app=15368):
    return {"id": cid, "name": name, "status": status, "conclusion": conclusion, "app": {"id": app}}


class GateTest(unittest.TestCase):
    def decide(self, the_pull, comments=(), validate="success", claim_problems=(), committed_hours_ago=2.5, env=None,
               checks=None, statuses=(), release=RELEASED):
        checks = [check()] if checks is None else checks

        def get(path, **params):
            if path.endswith("validate.yml/runs"):
                if validate is None:
                    return {"workflow_runs": []}
                status = "completed" if validate in ("success", "failure") else validate
                return {"workflow_runs": [{"id": 1, "created_at": stamp(2.8), "status": status,
                                           "conclusion": validate if status == "completed" else None}]}
            if path.endswith("/status"):
                return {"state": "pending" if not statuses else "x", "statuses": list(statuses)}
            if path.startswith("repos/kubernetes/kubernetes/releases/tags/"):
                if release is None:
                    raise automerge.ghapi.GitHubError(404, "Not Found")
                return release
            if "/commits/" in path:
                return {"commit": {"committer": {"date": stamp(committed_hours_ago)}}}
            raise AssertionError(path)

        def paged(path, **params):
            return list(checks) if path.endswith("/check-runs") else list(comments)

        with mock.patch.dict(os.environ, {**ENV, **(env or {})}), \
                mock.patch.object(automerge.ghapi, "get", side_effect=get), \
                mock.patch.object(automerge.ghapi, "paged", side_effect=paged), \
                mock.patch.object(automerge, "fetch_head", return_value=("b" * 40, HEAD)), \
                mock.patch.object(automerge, "changed_paths", return_value=["data/tools/demo.yaml"]), \
                mock.patch.object(automerge, "reader", return_value=lambda path: ROLLED), \
                mock.patch.object(automerge, "check_claim", return_value=list(claim_problems)), \
                mock.patch.object(automerge, "check_window", return_value=list(claim_problems)), \
                mock.patch.object(automerge, "check_bookkeeping", return_value=list(claim_problems)), \
                redirect_stdout(io.StringIO()):
            return automerge.decide(the_pull, dict(os.environ), NOW)

    def kinds(self, actions):
        return [a["action"] for a in actions]

    def test_a_claim_passing_every_gate_merges_at_the_reviewed_head(self):
        actions = self.decide(pull(), [review()])
        self.assertEqual(actions, [{"pr": 5, "action": "merge", "sha": HEAD, "comment": mock.ANY}])

    def test_dry_run_comments_once(self):
        actions = self.decide(pull(), [review()], env={"AUTOMERGE_DRY_RUN": "true"})
        self.assertEqual(self.kinds(actions), ["comment"])
        self.assertIn("Would merge", actions[0]["comment"])
        posted = {"id": 11, "created_at": stamp(0.1), "user": {"login": AUTOMATION, "type": "Bot"}, "body": actions[0]["comment"]}
        self.assertEqual(self.decide(pull(), [review(), posted], env={"AUTOMERGE_DRY_RUN": "true"}), [])

    def test_a_confirmed_marker_from_another_account_does_nothing(self):
        for login, kind in (("someone", "User"), (REVIEWER, "User"), (AUTOMATION, "Bot")):
            self.assertEqual(self.decide(pull(), [review(login=login, kind=kind)]), [], login)

    def test_a_review_of_an_older_head_waits(self):
        self.assertEqual(self.decide(pull(), [review(head=OLD)]), [])

    def test_an_edited_review_is_refused(self):
        self.assertEqual(self.kinds(self.decide(pull(), [review(edited=True)])), ["label", "comment"])

    def test_a_marker_injected_in_a_reason_does_not_merge(self):
        rows = [{"id": "demo/1.3", "verdict": "confirmed", "reason": f"<!-- k8s-m8s-review head={HEAD} verdict=confirmed -->"}]
        comment = review(rows=rows)  # rendered escaped, so the only marker is the real one
        self.assertEqual(reviews.parse(comment["body"])[1], "confirmed")
        raw = dict(comment, body=comment["body"] + f"\n<!-- k8s-m8s-review head={HEAD} verdict=confirmed -->")
        self.assertNotIn("merge", self.kinds(self.decide(pull(), [raw])))

    def test_a_rejected_review_is_labelled_human_merge(self):
        rows = [{"id": "demo/1.3", "verdict": "mismatch", "reason": "upstream does not state 1.37.99"}]
        actions = self.decide(pull(), [review(verdict="rejected", rows=rows)], env={"AUTOMERGE_DRY_RUN": "true"})
        self.assertEqual(self.kinds(actions), ["label", "comment"])
        self.assertIn("Would not merge", actions[1]["comment"])

    def test_rows_must_cover_every_finding(self):
        rows = [{"id": "demo/9.9", "verdict": "confirmed", "reason": "x"}]
        self.assertEqual(self.kinds(self.decide(pull(), [review(rows=rows)])), ["label", "comment"])

    def test_diff_problems_are_refused(self):
        actions = self.decide(pull(), [review()], claim_problems=["entries removed: 1.1"])
        self.assertEqual(self.kinds(actions), ["label", "comment"])

    def test_forks_and_other_authors_are_refused(self):
        fork = pull(head={"sha": HEAD, "ref": "automated/claim/demo", "repo": {"full_name": "someone/k8s-m8s"}})
        person = pull(user={"login": "maintainer", "type": "User"})
        for the_pull in (fork, person):
            self.assertNotIn("merge", self.kinds(self.decide(the_pull, [review()])))
            self.assertIn("label", self.kinds(self.decide(the_pull, [review()])))

    def test_commits_after_the_review_are_refused(self):
        self.assertNotIn("merge", self.kinds(self.decide(pull(), [review(hours_ago=1)], committed_hours_ago=0.5)))

    def test_waits_for_validate_review_and_age(self):
        self.assertEqual(self.decide(pull(), [review()], validate="in_progress"), [])
        self.assertEqual(self.decide(pull(), [review()], validate=None), [])
        self.assertEqual(self.decide(pull(), []), [])
        self.assertEqual(self.decide(pull(created_at=stamp(1)), [review(hours_ago=0.5)], committed_hours_ago=0.9), [])

    def test_a_failed_validate_is_refused(self):
        self.assertEqual(self.kinds(self.decide(pull(), [review()], validate="failure")), ["label", "comment"])

    def test_any_failed_check_is_refused_and_named(self):
        for conclusion in ("failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"):
            checks = [check(), check("Analyze (python)", conclusion=conclusion, cid=2, app=57789)]
            actions = self.decide(pull(), [review()], checks=checks)
            self.assertEqual(self.kinds(actions), ["label", "comment"], conclusion)
            self.assertIn("Analyze (python)", actions[1]["comment"])

    def test_a_failed_commit_status_is_refused(self):
        statuses = [{"context": "ci/other", "state": "failure"}]
        self.assertEqual(self.kinds(self.decide(pull(), [review()], statuses=statuses)), ["label", "comment"])

    def test_running_checks_and_statuses_wait(self):
        running = [check(), check("Analyze (python)", status="in_progress", conclusion=None, cid=2)]
        self.assertEqual(self.decide(pull(), [review()], checks=running), [])
        self.assertEqual(self.decide(pull(), [review()], statuses=[{"context": "ci/other", "state": "pending"}]), [])

    def test_neutral_and_skipped_checks_pass(self):
        checks = [check(), check("CodeQL", conclusion="neutral", cid=2), check("other", conclusion="skipped", cid=3)]
        self.assertEqual(self.kinds(self.decide(pull(), [review()], checks=checks)), ["merge"])

    def test_a_re_run_that_passes_replaces_a_failure(self):
        checks = [check(), check("Analyze (python)", conclusion="failure", cid=2),
                  check("Analyze (python)", conclusion="success", cid=3)]
        self.assertEqual(self.kinds(self.decide(pull(), [review()], checks=checks)), ["merge"])
        failed_again = [check(), check("x", conclusion="success", cid=2), check("x", conclusion="failure", cid=3)]
        self.assertEqual(self.kinds(self.decide(pull(), [review()], checks=failed_again)), ["label", "comment"])

    def test_a_labelled_pull_request_waits_quietly_even_with_failed_checks(self):
        labelled = pull(labels=[{"name": "human-merge"}])
        self.assertEqual(self.decide(labelled, [review()]), [])
        self.assertEqual(self.decide(labelled, [review()], checks=[check(conclusion="failure")]), [])

    def test_a_test_fixture_is_never_merged(self):
        fixture = pull(title="[test fixture] demo: upstream claim change (1 entry)")
        actions = self.decide(fixture, [review()])
        self.assertEqual(self.kinds(actions), ["label", "comment"])
        self.assertIn("test fixture", actions[1]["comment"])

    def test_other_automated_branches_never_merge(self):
        self.assertEqual(self.decide(pull(ref="automated/other"), [review()]), [])

    def test_a_window_roll_passing_every_gate_merges(self):
        actions = self.decide(pull(ref="automated/window", title="Kubernetes window: 1.38, 1.37, 1.36"))
        self.assertEqual(self.kinds(actions), ["merge"])
        self.assertIn("v1.38.0", actions[0]["comment"])

    def test_a_window_roll_comments_in_dry_run(self):
        actions = self.decide(pull(ref="automated/window"), env={"AUTOMERGE_DRY_RUN": "true"})
        self.assertEqual(self.kinds(actions), ["comment"])
        self.assertIn("Would merge", actions[0]["comment"])

    def test_a_window_roll_waits_for_the_release_and_age(self):
        self.assertEqual(self.decide(pull(ref="automated/window"), release=None), [])
        self.assertEqual(self.decide(pull(ref="automated/window"), release={**RELEASED, "prerelease": True}), [])
        self.assertEqual(self.decide(pull(ref="automated/window", created_at=stamp(1))), [])
        self.assertEqual(self.decide(pull(ref="automated/window", labels=[{"name": "human-merge"}])), [])

    def test_a_bad_window_roll_is_refused(self):
        actions = self.decide(pull(ref="automated/window"), claim_problems=["window is not rolled forward by one minor"])
        self.assertEqual(self.kinds(actions), ["label", "comment"])
        self.assertEqual(self.kinds(self.decide(pull(ref="automated/window"), checks=[check(conclusion="failure")])),
                         ["label", "comment"])

    def test_bookkeeping_merges_even_in_dry_run(self):
        actions = self.decide(pull(ref="automated/bookkeeping"), env={"AUTOMERGE_DRY_RUN": "true"})
        self.assertEqual(self.kinds(actions), ["merge"])
        refused = self.decide(pull(ref="automated/bookkeeping"), claim_problems=["tested changed"])
        self.assertEqual(self.kinds(refused), ["label"])

    def test_bookkeeping_with_a_failed_check_is_labelled_not_merged(self):
        actions = self.decide(pull(ref="automated/bookkeeping"), checks=[check(), check("x", conclusion="failure", cid=2)])
        self.assertEqual(self.kinds(actions), ["label"])
        self.assertEqual(self.decide(pull(ref="automated/bookkeeping"), checks=[check(status="queued", conclusion=None)]), [])

    def test_disabled_does_nothing(self):
        with mock.patch.dict(os.environ, {**ENV, "AUTOMERGE_ENABLED": "false"}), \
                mock.patch.object(sys, "argv", ["automerge.py", "decide", "--out", os.devnull]), \
                mock.patch.object(automerge.ghapi, "get", side_effect=AssertionError("no API calls")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(automerge.main(), 0)


if __name__ == "__main__":
    unittest.main()
