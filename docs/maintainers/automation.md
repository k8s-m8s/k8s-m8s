---
description: "How k8s-m8s checks its data against upstream daily: the workflows, the automated review and the merge gates."
---

# Automation

How k8s-m8s keeps its data in step with upstream without routine maintainer work, and the rules that keep that safe. Every claim change is still checked against its upstream source before it is published. A script proposes the change, Claude checks it in CI with no GitHub write access, and deterministic scripts post the verdict and merge. Maintainers handle only what the automation refuses or can't read.

## Who checks what

A claim change passes three separate checks before it can merge. Each one catches mistakes the others can't.

**1. Detect proposes the change (a parser).** Each tool has a hand-written parser in `scripts/extractors.py` that reads its upstream statement with fixed patterns. `detect.yml` runs it daily and opens a pull request for any difference. It is deterministic, cheap and reproducible: the same upstream file always gives the same result. But it only knows the patterns it was written for. If upstream changes so much that the parser can't read it, it raises an error, which becomes a `needs-human` issue. A subtler change can give a tidy, plausible change that is wrong, with no error at all. For example:
- the table gains a column, and the parser reads the wrong one;
- a footnote says a version was not tested, and the parser ignores footnotes;
- the heading changes from "Tested on" to "Expected to work with", so the claim should now be `stated`, not `tested`;
- the parser's pattern picks up versions from another table on the page.

**2. The review checks what the change means (Claude).** Claude never sees the parser's logic. It reads the proposed change and the upstream file at the recorded commit, and answers one question for each change: does upstream actually say this? It checks the version numbers, the claim level (`tested`, `stated` or `unsupported`) and the wording, and that nothing was inferred. It returns `confirmed`, `mismatch` or `ambiguous`, and it cannot change data or write to GitHub.

**3. The merge gates check the change's form (scripts).** Gate 4 in particular (`check_diff.py claim`) checks that only one tool's file changed, no entry was deleted, at most two entries became delisted and history is untouched, the sources are allowlisted, and the diff matches the findings exactly. It checks the shape of the change, not whether upstream says it. Every check on the pull request must also have passed.

| | Detect | Review | Merge gates |
|---|---|---|---|
| Checks | What changed upstream | Whether upstream says it | Whether the change is well-formed and in scope |
| Reads upstream | With fixed patterns | In full, in context | Not at all |
| Reliable at | Doing the same thing daily, at no cost | Meaning: tables, footnotes, headings, tested vs stated | Mechanical rules, exactly |
| Blind spot | Anything its patterns don't anticipate | Not reproducible run to run, so it never changes data itself | Meaning |

The rehearsal shows the split: the fixture adds `tested 1.37.99`, a version upstream never names. It is a well-formed change to one tool, so it passes every mechanical gate. Only the review, reading the actual README, can mark it `mismatch`.

Claude checks changes; it doesn't detect them. Detection has to be reproducible, so a change can be traced and re-run. It also covers every tool every day, while the review runs only when something changed.

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `validate.yml` (Validate data) | `pull_request`, push to `main`, manual | Validation, tests, site build. No secrets, read-only. The only workflow that runs pull request code. |
| `codeql.yml` (CodeQL) | `pull_request` unless every change is under `data/`, push to `main`, weekly, manual | Code scanning of the Python scripts and the workflows. No secrets; it can read the code and upload results. Builds and runs nothing (build-mode none). |
| `detect.yml` (Detect upstream changes) | daily 05:43 UTC, manual | Reads every upstream statement (`verify.py --claims-only`). Opens one claim pull request per tool, a window pull request, and `needs-human` issues; closes them when they no longer apply. From the same reading it refreshes `verified` and moved `source_commit` values on entries that still match (`--bookkeeping-out`, what `verify.py --write` would write), as one `automated/bookkeeping` pull request, leaving out tools with a change or problem waiting. |
| `review.yml` (Review claim changes) | after `validate.yml` on `automated/claim/**`, daily 07:13 UTC, manual | Claude checks each claim change against the upstream file at the recorded commit; `k8s-m8s-reviewer[bot]` posts the verdict. |
| `automerge.yml` (Merge automated pull requests) | reviewer comments, after `validate.yml` or `review.yml`, every 30 minutes, manual | Checks the merge gates and merges, labels or comments. |
| `pages.yml` (Deploy site) | push to `main`, manual | Builds and deploys the site. "Upstream last checked" comes from the data, so each day's bookkeeping merge deploys that day's date. |

Labels: `claim-change`, `window-change`, `bookkeeping`, `human-merge`, `needs-human`. `publish_findings.py` creates any that are missing.

## Identities and secrets

| Identity | Kind | Used by | Can do |
|---|---|---|---|
| `k8s-m8s-automation[bot]` | GitHub App, installed only on this repository, **no Workflows permission** | The publish steps of `detect.yml`, and the act step of `automerge.yml` | Push `automated/*` branches, open pull requests and issues, label, comment, close, merge. The only bypass actor on `main` and `automated/**`. |
| `k8s-m8s-reviewer[bot]` | GitHub App, installed only on this repository | The post step of `review.yml` | Read the repository and comment on pull requests. |
| Claude | `anthropics/claude-code-action`, authenticated with a Claude Code OAuth token | The review step of `review.yml` | Read files in the job workspace. Returns its verdict as structured output. No GitHub writes. |
| Maintainers | People | Everything else | Everything except bypassing the `main` and `automated` rulesets. |

| Environment | Secrets | Variables | Deployment branches |
|---|---|---|---|
| `automation` | `AUTOMATION_APP_PRIVATE_KEY` | `AUTOMATION_APP_CLIENT_ID` (the App's client ID) | `main` only |
| `review` | `CLAUDE_CODE_OAUTH_TOKEN`, `REVIEWER_APP_PRIVATE_KEY` | `REVIEWER_APP_CLIENT_ID` (the App's client ID) | `main` only |

| Repository variable | Value |
|---|---|
| `REVIEWER_LOGIN` | `k8s-m8s-reviewer[bot]` |
| `AUTOMATION_LOGIN` | `k8s-m8s-automation[bot]` |
| `AUTOMERGE_ENABLED` | `true` |
| `AUTOMERGE_DRY_RUN` | `false` (`true` makes claim and window pull requests comment instead of merging; see [dry run](#dry-run)) |

## Repository configuration

The automation also depends on these settings, which live in GitHub rather than in the repository's files. `tests/test_workflows.py` can't check them, so rerun the rehearsal (under [Routine operations](#routine-operations)) after changing any of them.

**GitHub Apps.** Both are installed on this repository only, with webhooks off.
- `k8s-m8s-automation`: Contents, Pull requests and Issues read and write; Actions, Checks and Metadata read. It has no Workflows permission, so it can't change files under `.github/workflows/`.
- `k8s-m8s-reviewer`: Pull requests read and write, and Metadata read. Nothing else.

**Environments.** `automation` and `review` hold the secrets and variables listed under [Identities and secrets](#identities-and-secrets). Both allow deployments from `main` only, with no required reviewers, so scheduled runs don't wait. GitHub creates an environment the first time a workflow names it, without that branch restriction. So an environment must exist before any workflow that uses it is merged.

**Rulesets.**
- `default`, on `main`: requires a pull request, with stale approvals dismissed on new pushes; requires the `validate` check on an up-to-date branch; requires linear history; blocks force pushes and deletion. The required approval count is set to fit the maintainer team, because GitHub doesn't let anyone approve their own pull request.
- `automated`, on `automated/**`: restricts creations, updates and deletions, and blocks force pushes.
- The automation App is the only bypass actor on both rulesets.

**Code owners.** `.github/CODEOWNERS` names the `@k8s-m8s/maintainers` team. GitHub ignores a code owner team that is secret or lacks write access.

**Code scanning.** CodeQL default setup is off; `codeql.yml` is the configuration (advanced setup). Default setup has no path filters, so it can't skip pull requests that only change data. GitHub rejects a workflow's CodeQL results while default setup is on, so leave it off.

**Claude token.** `CLAUDE_CODE_OAUTH_TOKEN` in the `review` environment is a long-lived Claude Code OAuth token, created with `claude setup-token`. It expires; see [Routine operations](#routine-operations).

### The actions allowlist

The allowlist decides which actions may run at all. The workflow files decide which version runs: every action there is pinned to a full commit SHA, which the repository setting below and `tests/test_workflows.py` both enforce. An upgrade is therefore a reviewed change to a workflow file, not a settings change.

The settings are under **Settings → Actions → General → Actions permissions**. They can also be set at organization level, which caps what each repository may allow:

- **Allow k8s-m8s, and select non-k8s-m8s, actions and reusable workflows**, with **Allow actions created by GitHub** ticked and **Allow actions by Marketplace verified creators** unticked. Actions created by GitHub include `github/codeql-action`, which `codeql.yml` uses.
- **Allow specified actions and reusable workflows**:
  ```
  anthropics/claude-code-action@*,
  oven-sh/setup-bun@*,
  ```
- **Require actions to be pinned to a full-length commit SHA**: on.

`oven-sh/setup-bun` is on the list because `claude-code-action` runs it internally, and the allowlist applies to nested actions too. A workflow that uses an action missing from the list fails at startup ("This run likely failed because of a workflow file issue") before any job runs. Add a new third-party action to the list only after deciding to trust its publisher, and add it to the allowlists in `tests/test_workflows.py` in the same pull request.

## Workflow rules

`tests/test_workflows.py` enforces these; it fails if a workflow breaks one.

1. **Jobs that hold secrets run from `main`.** A job that uses environment `automation` or `review`, mints an App token or reads a secret may be triggered only by `schedule`, `workflow_dispatch`, `workflow_run` or `issue_comment`. GitHub takes those workflow definitions from the default branch. No workflow uses `pull_request_target`. `validate.yml` runs on `pull_request` with no secrets and `contents: read`. `codeql.yml` also runs on `pull_request`, with no secrets, `contents: read`, and `security-events: write` to upload its results. It builds and runs nothing from the pull request and reads no configuration from it.
2. **Pull request content is data, never code.** In a job that holds secrets, every script runs from a `main` checkout (the workspace root, or `rules/` in `review.yml`). The pull request is checked out at its head SHA into `pr/` with `persist-credentials: false` and read with git, or read with `git show` from the `main` checkout. Nothing under `pr/` is executed, imported, sourced, installed or used as configuration. `review.yml` deletes `pr/` before Claude starts.
3. **An App token exists only in the step that mints it and the next step.** It is minted with `actions/create-github-app-token` directly before the step that uses it, with `permission-*` inputs and no `owner` or `repositories` (so it covers this repository only). It never reaches `GITHUB_ENV`, job `env` or job outputs.
4. **Jobs that hold tokens are kept small.** They use only `actions/checkout`, `actions/setup-python`, `actions/create-github-app-token`, `actions/download-artifact`, `anthropics/claude-code-action` (review job only) and repository scripts run from `main`. Every action is pinned by commit SHA. (`claude-code-action` itself uses `oven-sh/setup-bun` internally, pinned by SHA inside the action.) `tests/test_workflows.py` enforces this list. The repository's Actions policy also blocks third-party actions other than these two: see [the actions allowlist](#the-actions-allowlist).
5. **Dependencies are hash-pinned.** Jobs that hold secrets install with `pip install --require-hashes -r requirements.lock`, from `main`. Regenerate the lock after changing `requirements.txt`: `pip-compile --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url --output-file=requirements.lock requirements.txt`.
6. **Permissions are explicit.** Every workflow sets `permissions: {}` and grants permissions per job.
7. **No checkout keeps credentials** (`persist-credentials: false` everywhere). Commits are made through the GitHub API, so no git credentials are ever written.

## The review

`review.yml` runs for a pull request only if it is open, its head branch is `automated/claim/*` in this repository, its author is `AUTOMATION_LOGIN`, it has no `human-merge` label, and `validate.yml` passed on its head.

1. `main` is checked out to `rules/` and the pull request head to `pr/`.
2. `review_context.py` writes `context/findings.json` (from the pull request body's findings block), `context/diff.patch` and `context/pr.json`. It stops the review if the head moved or if the diff and the findings disagree.
3. `fetch_sources.py` downloads each finding's source at `source_commit` into `upstream/<tool>/<commit>/<path>` and writes `upstream/index.json`. For a removal it also downloads the source at `removed_commit`, where the row is gone, and records that commit's date. It only fetches sources under the tool's `repo` or `official_sources` as `main` records them, contacts only `raw.githubusercontent.com` and `api.github.com`, and skips files over 2 MB.
4. `pr/` is deleted, and `docs/maintainers/claude-review.md` is copied to the workspace root.
5. Claude runs with `--permission-mode default`, `--allowedTools "Read,Glob,Grep"`, a deny list that covers Bash, file writes, web tools, subagents and the action's GitHub tools, `--max-turns 40`, a pinned model, and the read-only `github.token`. It returns its verdict through `--json-schema`, which is the same schema as `schema/review.schema.json`.
6. `post_review.py render` accepts the verdict only if it matches the schema with exactly one row per finding, no reason contains a marker or looks like a credential, and Claude's session log shows only Read, Glob, Grep and the structured output tool, reading only `context/`, `upstream/`, `rules/` or the instructions file. The head SHA in the marker comes from `context/pr.json`, never from Claude. Reasons are HTML-escaped.
7. Only then is the reviewer App's token minted, with `permission-pull-requests: write`, and `post_review.py post` posts the comment.

## Merge gates

`automerge.yml` checks `AUTOMERGE_ENABLED` first. The gates run with the read-only workflow token, from the `main` checkout, reading the pull request with `git fetch` and `git show`. The automation App's token is minted only if there is something to do.

Every automated pull request needs every check on its head commit to have passed: `validate.yml`, which must have run, and any other check run or commit status (only the newest run of each check counts, so a re-run can replace a failure). While a check is running the pull request waits. If one finished any other way than success, neutral or skipped, it is labelled `human-merge` with a comment naming the check: re-run it, and once it passes remove the label.

**Bookkeeping (`automated/bookkeeping`)** merges, even in dry run, when the author is `AUTOMATION_LOGIN`, every check passed on the head, and `check_diff.py bookkeeping` passes. That check passes only if the only differences are `verified` and `source_commit` values on existing entries of `data/tools/*.yaml` and in `data/kubernetes.yaml`. A failure adds `human-merge`.

**Claim changes (`automated/claim/*`)** merge only when every gate passes:

1. The author is `AUTOMATION_LOGIN` (a Bot account), and the head branch is `automated/claim/*` in this repository, not a fork.
2. There is no `human-merge` label.
3. Every check passed on the head commit, including `validate.yml`.
4. `check_diff.py claim` passes:
   - exactly one `data/tools/<tool>.yaml` changed, and nothing else (so nothing under `.github/`, `scripts/`, `schema/`, `docs/`, `.claude/`, `CLAUDE.md`, `zensical.toml` or `requirements*`);
   - the file's header (name, repo, `official_sources`, notes) is unchanged;
   - no entry was deleted, and at most 10 entries changed;
   - an entry becomes delisted (a removal) only if its finding names a full `removed_commit`, the upstream commit where the row is gone. A plain removal changes nothing but `delisted`; a row read from upstream's history may also take the last values upstream listed. At most 2 removals per pull request, and at least one current entry must remain;
   - no entry that is already delisted changes, and no entry is added already delisted;
   - every changed entry has a source under the tool's `repo` or `official_sources` (as `main` records them) and a full `source_commit`;
   - the changed entries are exactly the findings block's, with its old and new values;
   - the releases block is in the exact layout the writer produces, so nothing hides in comments.
5. The newest comment by `REVIEWER_LOGIN` (account type `Bot`) is unedited, and contains exactly one `<!-- k8s-m8s-review head=<sha> verdict=confirmed -->` marker, with `<sha>` equal to the current head.
6. That comment has a `confirmed` row for every finding id, and no other rows.
7. The head commit is older than that comment.
8. The pull request has been open for at least 2 hours.
9. It is not a `[test fixture]` pull request. This is checked last, so a rehearsal still shows the review's own verdict.

**Window changes (`automated/window`)** merge only when every gate passes:

1. The author is `AUTOMATION_LOGIN` (a Bot account), and the head branch is `automated/window` in this repository.
2. There is no `human-merge` label.
3. Every check passed on the head commit, including `validate.yml`.
4. `check_diff.py window` passes: only `data/kubernetes.yaml` changed, and only its `window`, `source_commit` and `verified`; the new window is the old one rolled forward by exactly one minor (`["1.38", "1.37", "1.36"]` after `["1.37", "1.36", "1.35"]`); `source_commit` is a full commit; and the one `kubernetes/window` finding has the diff's old and new values.
5. Kubernetes has published the new minor's first release: the `kubernetes/kubernetes` GitHub release `v<minor>.0` exists and is not a draft or a pre-release. Until then the pull request waits. This is a second source: the window itself comes from the release schedule data, where `verify.py` counts a minor only once its `releaseDate` has come.
6. The pull request has been open for at least 2 hours.

When every gate passes, the job squash-merges at the checked head SHA, comments (with a link to the review, or to the Kubernetes release), and deletes the branch. If a gate that needs a maintainer fails (a failed check, gate 4, an edited or invalid review, a `rejected` verdict or missing rows, commits after the review, a fixture), it adds `human-merge` and comments once with the reason. A pull request that is waiting (checks running, no review for this head yet, release not published, under 2 hours old) is left alone for up to 24 hours after its head commit. After that it is labelled `human-merge`, with a comment saying what it is still waiting for, so a review that never comes (for example because the Claude token expired) or a check that never finishes doesn't go unnoticed. Pull requests opened by people are never merged automatically.

A claim or window pull request that gate 4 or the window check would refuse is labelled `human-merge` when it is opened, with the reason in its description, so it is not reviewed or merged automatically.

### Dry run

With `AUTOMERGE_DRY_RUN=true`, claim and window pull requests get a "Would merge" or "Would not merge, reason: …" comment instead of a merge, and are still labelled `human-merge` on refusal. Bookkeeping pull requests merge either way. Dry run was on for a shadow period from the automation's start until late November 2026, while maintainers merged by hand and compared with the comments; they never disagreed. Turn it on again to watch a change to the gates without merging.

## Routine operations

**Pause all automation.** Set `AUTOMERGE_ENABLED` to `false`, which stops every automated merge. Then disable `review.yml` (and, if needed, `detect.yml`) in the Actions tab.

**Revert a bad merge.** Revert the squash commit through a pull request. If upstream still says the same thing, the next daily run opens the claim pull request again, and it goes through the review again.

**A `needs-human` issue.** Fix the extractor or the data in a pull request, as for any other change. The issue closes itself on the first daily run where the problem is gone.

**A pull request labelled `human-merge`.** Its comment or description gives the reason. For a window pull request, follow the window roll runbook in `CONTRIBUTING.md`. For a removal, check both linked commits: the row is listed at the first and gone at the second. If the reason is "still waiting after 24 hours", fix what it waits for: re-run the review (`review.yml` with the pull request number) or the check, or renew the token, then remove the label.

**Renew tokens.**
- `CLAUDE_CODE_OAUTH_TOKEN`: run `claude setup-token` again before the expiry it showed, and replace the secret in the `review` environment.
- App private keys don't expire. Rotate a key by generating a new one in the App's settings, replacing the environment secret, then deleting the old key.

**Rehearse end to end.** Run `detect.yml` manually with the `fixture` input set to a tool name (for example `velero`). With `fixture_kind` `claim`, it opens a `[test fixture]` claim pull request that adds a `tested` patch version upstream does not state (`<newest minor>.99`). With `removal`, it marks the newest entry delisted, naming as the commit where the row is gone a commit that still lists it. Either way the review must mark it `mismatch`, and `automerge.yml` must say "Not merging" (or "Would not merge" in dry run) and add `human-merge`. Gate 9 refuses a fixture even if the review were wrong. Close the pull request afterwards. The next `detect.yml` run deletes its branch, as it does for any automated branch whose pull request was closed by hand.

**Update the actions.** Dependabot proposes action and pip updates monthly, grouped. They never merge automatically. Review updates to `anthropics/claude-code-action`, `actions/create-github-app-token` and Zensical line by line. For `claude-code-action`, check the release notes for changes to permission modes, `allowed_bots`, `structured_output` and `execution_file`. Then re-run the fixture rehearsal.

## Design notes

Why some parts work the way they do:

- **Claude returns structured output and has no Write tool.** A deny on `Edit` also covers `Write` in Claude Code, and structured output needs no file access at all.
- **Claude's working directory is the workspace root.** `claude-code-action` has no working-directory input. So `main` is checked out to `rules/`, and `pr/` is deleted before Claude starts. The workspace then holds only `main`'s files (which carry no Claude configuration) and the review inputs.
- **`--permission-mode default` is passed explicitly.** Newer versions of the action default to auto mode, where `--allowedTools` is not a strict list.
- **`allowed_bots` names the automation App.** Agent mode refuses runs started by a bot, and a `workflow_run` after the automation App's push counts as one.
- **The reviewer token is minted by `create-github-app-token`.** A script can't sign the App's JWT with the standard library alone. So `post_review.py` is split into `render` (no token) and `post`, with the token minted between them.
- **Claim pull requests are written with `verify.py --claims-only`.** `--apply` refreshes `verified` on every entry, which would make a claim pull request touch the whole file and change every day. So `detect.yml` reads upstream once and writes it two ways: the claim changes, and with `--bookkeeping-out` what `--write` would make of the same reading, for the bookkeeping pull request. One reading takes about 280 GitHub API requests, of the 1,000 an hour the workflow token allows.
- **Bookkeeping leaves out tools with a change or problem waiting.** Refreshing a tool's file while its claim pull request is open would conflict with it: the claim pull request would be rebuilt and reviewed again every day, and might never merge. Those tools keep their older date, which the site shows as a warning, until the change merges or the problem is fixed. A failed bookkeeping step never stops the claim changes; that day's dates are just not refreshed.
- **"Upstream last checked" comes from the data.** Each tool page shows the newest `verified` date of its current entries. An entry only gets a date from a complete, successful read of its tool, and on that day every other entry matched too (bookkeeping refreshes a tool only when all of them match, and a claim change carries every difference the read found), so the whole page was compared on that date. A merged claim change therefore makes its tool current at once. A change that only delists rows dates no current entry, so its tool catches up on the next daily check. The matrix shows the date most tools were last checked, which is the latest daily check that completed, and names up to three tools behind it (more are only counted). It is not the newest date of any tool: a claim change merged on a day whose bookkeeping did not would otherwise make every other tool look behind. A local build shows what the site shows, and a day whose check did not complete shows the older date rather than claiming a check that did not happen.
- **Automated pull requests contain only data files.** The generated pages (`docs/matrix.md`, `docs/tools/`) are not committed.
- **Findings have one id per entry,** `<tool>/<version>`.
- **A removal names the commit where the row is gone.** `removed_commit` is the commit at which upstream's statement was read without the row (for a row read from upstream's history, the commit that replaced it). It is evidence for the review, never written to the data. When no commit read that day shows the removal (for example, the whole source file is gone), it is empty and gate 4 leaves the change to a maintainer.
- **Window changes have no Claude review.** The question is mechanical (is the minor released, and is the window the latest three), so it is checked twice by scripts, against the release schedule data and against Kubernetes' own release record.
- **`CODEOWNERS` names the `@k8s-m8s/maintainers` team** rather than a personal account.
