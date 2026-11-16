# Contributing

Thanks for helping keep the data accurate.

k8s-m8s competes on rigour, not coverage. It is only useful if every claim is sourced and nothing is inferred, so the rules below are applied strictly.

## The one rule

Every claim must come from the tool's own maintainers, in an official source (the tool's repository, documentation or release notes), and the pull request must link to it.

Contributions based on inference, personal testing or third-party compilations cannot be accepted, however likely they are to be right.

## Evidence rules

1. **Official upstream sources only**: the tool's own repository, documentation or release notes. Never blogs, vendors, forums or other compilations. Validation enforces this: every `source` must sit under the tool's `repo` or one of its `official_sources`.
2. **Four claim levels**, derived from the data:
   - `tested`: upstream says it tested this tool version on this Kubernetes version. Keep exact patch versions; if upstream names only the minor, record the minor and never invent a patch.
   - `stated`: upstream says it is supported or expected to work, without saying it was tested. Record the range with upstream's own `wording`.
   - `unsupported`: upstream explicitly says it does not work or is not supported. Record single minors or ranges.
   - `unknown`: upstream says nothing covering the combination. This is the default; no field is needed.
3. **Never infer, extend or backfill.** Not from client-go versions, CI configuration, release dates, or "it probably works". If upstream is silent or ambiguous, leave it unknown and say so in the pull request.
4. **Never upgrade `stated` to `tested`.** A range like "1.18-latest" is `stated` only.
5. **Provenance on every release entry**: `source` (URL), `source_commit` and `verified` (the date it was last read). `source_commit` is the upstream commit that last changed the source file, as of that date, so it only moves when upstream edits the statement. For a statement in release notes it is the commit the release tag points to.
6. **Read sources from the repository at a specific commit** (a git clone or the GitHub API), not from a rendered or cached web page. Cached pages have been stale before.
7. **One entry per tool version.** Validation rejects duplicates.
   - Use `version_range` (a band such as `min: "0.18"`) only when upstream itself states its claim for a band of versions. Never expand a band into versions or merge versions into a band. Bands must not overlap each other or a single-version entry.
   - The one exception is an upstream table keyed by Kubernetes version that gives a minimum tool version for each (Karpenter). It may be transposed into bands that stop at the highest tool version upstream names, record only the compatible combinations, and are labelled as transposed in `notes`.
8. **Never delete history.** When upstream removes a row, add `delisted: <date>` to the entry and keep its last known values.
9. **Facts, not prose.** Record version numbers and claims in the schema. Do not paste upstream tables, paragraphs or release notes. `wording` holds only the short phrase upstream uses for its claim.

## Correcting or updating a tool

1. Edit `data/tools/<tool>.yaml`.
2. Set `source` to the upstream URL, `source_commit` to the upstream commit that last changed the source file, and `verified` to today's date. `python scripts/verify.py --write <tool>` fills in the last two for entries that match upstream.
3. Run:
   ```bash
   pip install -r requirements.txt
   python scripts/validate.py --strict
   python scripts/generate.py
   python -m unittest discover tests
   ```
4. Run `python scripts/verify.py <tool>` to confirm the file matches upstream. Set `GITHUB_TOKEN` to avoid GitHub API rate limits.
5. Open a pull request for that one tool. Quote the source URL and commit, and list what changed.

## Adding a tool

1. Find the upstream compatibility statement. If there is none, the tool cannot be added yet.
2. Note the upstream repository's license in `upstream_license`.
3. If the statement lives in the project's documentation and not in its main repository, add that location to `official_sources`. Only the project's own sites and repositories belong there, and the pull request must show that it is the project's (for example, it is linked from the main repository's README).
4. Read the statement from the upstream repository at its head commit and write `data/tools/<name>.yaml`. The file name must match `name`. Copy an existing file as a starting point.
5. Add an extractor for the tool to `scripts/extractors.py`, so the entry can be re-checked automatically, and confirm `python scripts/verify.py <name>` reports every entry as matching.
6. Run the commands above and check the generated page reads correctly. `generate.py` also updates the navigation in `zensical.toml`; commit that change with the tool.
7. One tool per pull request.

A weekly workflow re-reads every upstream statement with `verify.py --apply` and opens a pull request with any differences. Those pull requests follow the same rule as any other: each change is checked against the linked upstream source before it is merged.

When a new Kubernetes minor is released, `data/kubernetes.yaml` is updated first and every tool is re-verified. New columns start as unknown unless an existing `stated` range covers them.

## Field guide

| Field | Use |
|---|---|
| `tested` | Kubernetes versions upstream says it tested, as precisely as upstream states them: a patch version such as `"1.37.1"`, or a minor such as `"1.37"` when upstream names no patch |
| `stated` | A range upstream says is supported or expected, with upstream's own short `wording`. Use `max: latest` for an open-ended range and omit `min` when upstream gives no lower bound |
| `unsupported` | Kubernetes minors, or ranges of minors (`min`/`max`), upstream explicitly says are not supported |
| `version` | The tool's release line (`"1.19"`), or a patch release only when upstream states its claim per patch release |
| `version_range` | Used in place of `version` only when upstream states its claim for a band of tool versions, such as "0.18 and later" (`min: "0.18"`) |
| `official_sources` | The project's own documentation sites or repositories, when the statement is not in its main repository. Every `source` must sit under `repo` or one of these, or validation fails |
| `delisted` | Date a row was removed from the upstream source. Keep the entry; do not delete it |

Anything not covered by these fields is shown as unknown.

## Conventions

- Version strings are always quoted in YAML (`"1.19"`, not `1.19`).
- Dates are ISO 8601 (`2026-11-15`).
- `docs/matrix.md` and `docs/tools/` are generated; never edit them by hand.
- Do not add project logos or trademarks.
- Python: standard library plus PyYAML and jsonschema only. Keep scripts small.

## Scope

In: Kubernetes compatibility of tool versions, for the latest three Kubernetes minors. Older data is kept as history.

Out for now: Helm chart versions, image lists, release-note summaries, tool-to-tool compatibility, pre-release Kubernetes versions, cloud-provider extended support, community reports.

## Please don't

- Paste upstream tables or paragraphs. Record the facts in the schema and link to the source.
- Copy data from other compatibility data sets.
- Change a `stated` claim to `tested` without an upstream statement that it was tested.

## Licensing of contributions

By contributing you agree that your code contributions are licensed under Apache-2.0 and your data and documentation contributions under CC BY 4.0.
