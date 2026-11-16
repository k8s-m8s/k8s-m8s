# k8s-m8s

Which versions of popular Kubernetes tools work with which Kubernetes versions, with the receipts.

For every tool version, k8s-m8s records what the maintainers actually claimed, links to where they said it, and shows when it was last checked. Nothing is inferred.

- [Compatibility matrix](matrix.md): every tool against the latest three Kubernetes versions.
- [How to read this](how-to-read.md): what tested, stated, unsupported and unknown mean.
- Each tool has its own page, listed in the navigation, with every recorded version and a link to the upstream source at the commit it was read from.

The data behind these pages is one YAML file per tool, checked against a published [JSON Schema](schema/tool.schema.json) and re-verified against upstream every week.

k8s-m8s is an independent project. It is not affiliated with or endorsed by the Kubernetes project, the Cloud Native Computing Foundation, the Linux Foundation, or any listed tool. Always check the linked upstream source and test before upgrading a cluster or a tool.
