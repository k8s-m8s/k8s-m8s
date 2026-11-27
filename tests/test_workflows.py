"""Static checks on .github/workflows/*.yml: the workflow rules in docs/maintainers/automation.md."""

import json
import re
import shlex
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
SECRET_ENVIRONMENTS = {"automation", "review"}
SAFE_TRIGGERS = {"schedule", "workflow_dispatch", "workflow_run", "issue_comment"}
PINNED = re.compile(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}")
# actions any job may use
ALLOWED = {"actions/checkout", "actions/setup-python", "actions/upload-artifact", "actions/download-artifact",
           "actions/create-github-app-token", "anthropics/claude-code-action",
           "actions/configure-pages", "actions/upload-pages-artifact", "actions/deploy-pages",
           "github/codeql-action/init", "github/codeql-action/analyze"}
# codeql.yml may also upload its results; it holds no secrets and runs no repository code
PULL_REQUEST_PERMISSIONS = {"codeql.yml": {"contents": "read", "security-events": "write"}}
# actions a job holding a token or secret may use (rule 4)
ALLOWED_WITH_SECRETS = {"actions/checkout", "actions/setup-python", "actions/create-github-app-token",
                        "anthropics/claude-code-action", "actions/download-artifact"}
APP_TOKEN = "actions/create-github-app-token"
CLAUDE = "anthropics/claude-code-action"


def load(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads the key `on` as true
    return data


def triggers(workflow):
    on = workflow["on"]
    return {on} if isinstance(on, str) else set(on)


def environment(job):
    env = job.get("environment")
    return env.get("name") if isinstance(env, dict) else env


def action(step):
    return step["uses"].split("@")[0] if "uses" in step else None


def holds_secrets(job):
    text = json.dumps(job)
    return environment(job) in SECRET_ENVIRONMENTS or "secrets." in text or any(
        action(s) in (APP_TOKEN, CLAUDE) for s in job.get("steps", []))


def every_job():
    for path in WORKFLOWS:
        workflow = load(path)
        for name, job in workflow["jobs"].items():
            yield path.name, workflow, name, job


class WorkflowRulesTest(unittest.TestCase):
    def test_every_workflow_has_empty_top_level_permissions(self):
        for path in WORKFLOWS:
            self.assertEqual(load(path).get("permissions"), {}, path.name)

    def test_every_action_is_pinned_and_allowlisted(self):
        for file, _, name, job in every_job():
            for step in job.get("steps", []):
                if "uses" in step:
                    self.assertRegex(step["uses"], PINNED, f"{file}:{name}")
                    self.assertIn(action(step), ALLOWED, f"{file}:{name}")

    def test_secret_holding_jobs_run_only_from_main(self):
        for file, workflow, name, job in every_job():
            if holds_secrets(job):
                unsafe = triggers(workflow) - SAFE_TRIGGERS
                self.assertFalse(unsafe, f"{file}:{name} holds secrets but runs on {sorted(unsafe)}")

    def test_secret_holding_jobs_use_only_the_rule_4_actions(self):
        for file, _, name, job in every_job():
            if holds_secrets(job) and environment(job) != "github-pages":
                for step in job.get("steps", []):
                    if "uses" in step:
                        self.assertIn(action(step), ALLOWED_WITH_SECRETS, f"{file}:{name}")
                    if action(step) == CLAUDE:
                        self.assertEqual(environment(job), "review", f"{file}:{name}")

    def test_secret_holding_jobs_install_hash_pinned_dependencies(self):
        for file, _, name, job in every_job():
            if holds_secrets(job) and environment(job) != "github-pages":
                for step in job.get("steps", []):
                    if "pip install" in step.get("run", ""):
                        self.assertIn("--require-hashes", step["run"], f"{file}:{name}")
                        self.assertNotRegex(step["run"], r"-r\s+pr/", f"{file}:{name}")

    def test_nothing_under_pr_is_run(self):
        for file, _, name, job in every_job():
            if not holds_secrets(job):
                continue
            for step in job.get("steps", []):
                script = step.get("run", "")
                self.assertNotEqual(step.get("working-directory", "").strip("./"), "pr", f"{file}:{name}")
                self.assertNotRegex(script, r"(?<![\w-])\.?/?pr/", f"{file}:{name}: refers to a path under pr/")
                self.assertNotRegex(script, r"\bcd\s+pr\b", f"{file}:{name}")

    def test_app_tokens_live_only_in_the_minting_and_next_step(self):
        for file, workflow, name, job in every_job():
            self.assertNotIn("secrets.", json.dumps(job.get("env", {})), f"{file}:{name} job env")
            self.assertNotIn("secrets.", json.dumps(workflow.get("env", {})), f"{file} workflow env")
            steps = job.get("steps", [])
            for index, step in enumerate(steps):
                if holds_secrets(job):
                    self.assertNotIn("GITHUB_ENV", step.get("run", ""), f"{file}:{name}")
                if action(step) != APP_TOKEN:
                    continue
                self.assertTrue(step.get("id"), f"{file}:{name}: token step needs an id")
                self.assertNotIn("owner", step.get("with", {}), f"{file}:{name}: token must be for this repo only")
                self.assertNotIn("repositories", step.get("with", {}), f"{file}:{name}")
                self.assertTrue(any(k.startswith("permission-") for k in step["with"]), f"{file}:{name}: no permission inputs")
                self.assertNotIn("permission-workflows", step["with"], f"{file}:{name}")
                reference = f"steps.{step['id']}.outputs.token"
                users = [i for i, s in enumerate(steps) if reference in json.dumps(s)]
                self.assertEqual(users, [index + 1], f"{file}:{name}: the token must be used by the next step only")
                self.assertNotIn(reference, json.dumps(job.get("outputs", {})), f"{file}:{name}")

    def test_pull_request_runs_get_no_secrets_and_read_only_access(self):
        for file, workflow, name, job in every_job():
            if triggers(workflow) & {"pull_request", "pull_request_target", "push"} and file != "pages.yml":
                self.assertFalse(holds_secrets(job), f"{file}:{name}")
                self.assertIsNone(environment(job), f"{file}:{name}")
                expected = PULL_REQUEST_PERMISSIONS.get(file, {"contents": "read"})
                self.assertEqual(job.get("permissions"), expected, f"{file}:{name}")
            self.assertNotIn("pull_request_target", triggers(workflow), file)

    def test_codeql_skips_data_only_pull_requests_and_builds_nothing(self):
        workflow = load(ROOT / ".github/workflows/codeql.yml")
        self.assertEqual(workflow["on"]["pull_request"]["paths-ignore"], ["data/**"])
        self.assertNotIn("paths", workflow["on"]["pull_request"])
        self.assertFalse({"paths", "paths-ignore"} & set(workflow["on"]["push"]))  # push runs stay complete
        for job in workflow["jobs"].values():
            init = next(s for s in job["steps"] if action(s) == "github/codeql-action/init")
            self.assertEqual(init["with"]["build-mode"], "none")
            self.assertNotIn("config-file", init["with"])  # no configuration read from the checkout

    def test_pages_runs_no_pull_request_code(self):
        workflow = load(ROOT / ".github/workflows/pages.yml")
        self.assertEqual(triggers(workflow), {"push", "workflow_run", "workflow_dispatch"})
        self.assertEqual(workflow["on"]["push"], {"branches": ["main"]})


class ReviewJobTest(unittest.TestCase):
    def setUp(self):
        self.job = load(ROOT / ".github/workflows/review.yml")["jobs"]["review"]
        self.claude = next(s for s in self.job["steps"] if action(s) == CLAUDE)

    def args(self):
        return shlex.split(self.claude["with"]["claude_args"])

    def option(self, flag):
        args = self.args()
        return args[args.index(flag) + 1]

    def test_claude_has_no_write_access(self):
        self.assertEqual(self.claude["with"]["github_token"], "${{ github.token }}")
        self.assertNotIn("id-token", self.job["permissions"])
        self.assertTrue(all(v == "read" for v in self.job["permissions"].values()))

    def test_claude_has_no_shell_network_or_file_writes(self):
        self.assertEqual(self.option("--permission-mode"), "default")
        self.assertEqual(set(self.option("--allowedTools").split(",")), {"Read", "Glob", "Grep"})
        denied = set(self.option("--disallowedTools").split(","))
        self.assertLessEqual({"Bash", "Edit", "Write", "WebFetch", "WebSearch", "NotebookEdit"}, denied)
        self.assertIn("--model", self.args())
        self.assertIn("--max-turns", self.args())

    def test_the_inline_schema_is_the_published_one(self):
        schema = json.loads((ROOT / "schema/review.schema.json").read_text())
        for key in ("$schema", "$id", "title"):
            schema.pop(key)
        self.assertEqual(json.loads(self.option("--json-schema")), schema)

    def test_the_pull_request_is_deleted_before_claude_runs(self):
        steps = self.job["steps"]
        claude_at = steps.index(self.claude)
        self.assertTrue(any("rm -rf pr" in s.get("run", "") for s in steps[:claude_at]))
        checkout_pr = next(s for s in steps if action(s) == "actions/checkout" and s["with"].get("path") == "pr")
        self.assertIs(checkout_pr["with"]["persist-credentials"], False)

    def test_secret_steps_run_only_after_the_inputs_succeeded(self):
        for step in self.job["steps"][self.job["steps"].index(self.claude) + 1:]:
            self.assertIn("steps.context.outcome == 'success'", step.get("if", ""), step.get("name") or step.get("id"))


class CheckoutTest(unittest.TestCase):
    def test_no_checkout_keeps_credentials(self):
        for file, _, name, job in every_job():
            for step in job.get("steps", []):
                if action(step) == "actions/checkout":
                    self.assertIs(step.get("with", {}).get("persist-credentials"), False, f"{file}:{name}")


if __name__ == "__main__":
    unittest.main()
