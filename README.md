# k8s-m8s

**Kubernetes mates.** Which versions of popular Kubernetes tools work with which Kubernetes versions, with the receipts.

For every tool version, k8s-m8s records what the maintainers actually claimed, links to where they said it, and shows when it was last checked.

Browse it at <https://docs.k8s-m8s.io/>.

## How to read the data

| Level | Meaning |
|---|---|
| **tested** | The maintainers state they tested this tool version on this Kubernetes version. Exact patch versions are shown where the maintainers give them. |
| **stated** | The maintainers state it is supported or expected to work, without saying it was tested. |
| **unsupported** | The maintainers explicitly state it does not work or is not supported. |
| **unknown** | The maintainers say nothing that covers this combination. |

Nothing is inferred. If upstream is silent, the answer is unknown.

## What's here

- `data/tools/` holds one YAML file per tool. This is the source of truth.
- `data/kubernetes.yaml` holds the current Kubernetes window (the latest three minor versions).
- `schema/` holds the JSON Schema the data is validated against.
- `scripts/` validates the data, generates the Markdown pages under `docs/`, and re-checks the data against upstream.
- `docs/` and `zensical.toml` are the site, built with [Zensical](https://zensical.org).

## Using it locally

```bash
pip install -r requirements.txt
python scripts/validate.py --strict
python scripts/generate.py
python -m unittest discover tests
```

`generate.py` writes `docs/matrix.md` and one page per tool in `docs/tools/`.

To preview the site (needs Python 3.11 or newer):

```bash
pip install -r requirements-site.txt
zensical serve
```

`python scripts/verify.py` re-reads every upstream statement through the GitHub API and reports where the recorded data no longer matches. Set `GITHUB_TOKEN` to avoid rate limits.

## How the data stays current

Every day a workflow re-reads each tool's upstream statement. When a claim changes, it opens a pull request for that tool, listing old and new values with links to the source at the commit it was read from. An automated review checks each change, including rows upstream has removed, against that upstream file before it can merge. A new Kubernetes minor joins the matrix once Kubernetes has published its release. Maintainers review anything the review or the checks flag. A weekly workflow refreshes the verification dates. [docs/maintainers/automation.md](docs/maintainers/automation.md) has the details.

## Corrections

If you maintain a listed tool, or spot an error, open an issue or pull request with a link to the upstream statement. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

k8s-m8s is an independent project. It is not affiliated with or endorsed by the Kubernetes project, the Cloud Native Computing Foundation, the Linux Foundation, or any listed tool. Listed projects have not reviewed this data unless stated. Kubernetes is a registered trademark of The Linux Foundation.

The data is provided as is. Always check the linked upstream source and test before upgrading a cluster or a tool.

## License

- Code (scripts, schema, workflows): [Apache-2.0](LICENSE)
- Data and documentation (`data/`, `docs/`): [CC BY 4.0](LICENSE-DATA). To credit it, cite "k8s-m8s contributors" with a link to <https://docs.k8s-m8s.io/>.
