# How to read this

Each cell answers one question: what have this tool's maintainers said about running this version on this Kubernetes version?

| Level | Meaning |
|---|---|
| <span class="k8s-level k8s-tested">tested</span> | The maintainers state they tested this combination. The Kubernetes versions they tested are shown next to the label, as precisely as they state them: a patch version where one is given, otherwise the minor. |
| <span class="k8s-level k8s-stated">stated</span> | The maintainers state it is supported or expected to work, but do not say they tested it. |
| <span class="k8s-level k8s-unsupported">unsupported</span> | The maintainers explicitly state it does not work or is not supported. |
| <span class="k8s-level k8s-unknown">unknown</span> | The maintainers say nothing that covers this combination. |

## What this means in practice

- **tested** is the strongest evidence available from upstream. It still refers to specific patch versions, which may not be yours, or to a minor without saying which patch.
- **stated** is a claim, not a test result. Many projects publish a wide range such as "1.18 to latest" alongside a much shorter tested list. Treat these combinations as likely to work but unverified, and test them yourself.
- **unknown** does not mean incompatible. It means there is no upstream statement to point to.

## Rows and versions

- The [compatibility matrix](matrix.md) lists only tool versions that have an upstream statement covering one of the Kubernetes versions shown. The first table on a tool's page does the same, and the table below it lists every recorded version, including older ones, with what upstream stated for each.
- Most rows are a release line such as 1.19. Where a project publishes its statement for each patch release, the rows are patch releases such as 1.33.8, because the claims can differ between them.
- A row such as "0.18 and later" or "1.13 to 1.14" is a band of versions. It is used when the project states one claim for a whole band and not for each version.
- On tool pages, "Stated range" is the range of Kubernetes versions the project says is supported or expected to work, and "Stated unsupported" is the range it explicitly says is not. "up to 1.20" means the project gave no lower bound; "latest" means it left the range open-ended.
- Each tool page starts with a short note on how that project publishes its statement, including anything that could not be recorded.

## Where the data comes from

Only from each tool's own repository, documentation or release notes. Every row on a tool page links to the upstream statement as it stood at the commit it was read from, and shows the date it was last verified. The link is permanent, so it keeps matching the row even after the project edits its page; follow it to GitHub to see the current version. Nothing is inferred from dependency versions, CI configuration or other people's data sets.

## History

Projects often drop older versions from their compatibility tables. When that happens the row is kept on the tool's page under "No longer listed upstream", with the last values that were published.

## Corrections

If something is wrong or out of date, [open a data correction issue](https://github.com/k8s-m8s/k8s-m8s/issues/new/choose) or a pull request on the [repository](https://github.com/k8s-m8s/k8s-m8s), with a link to the upstream statement.
