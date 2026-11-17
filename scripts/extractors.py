"""Read each tool's upstream compatibility statement.

One function per tool, registered in EXTRACTORS under the tool's name. Each
returns the release entries upstream currently publishes, in the same shape as
data/tools/<tool>.yaml (without `verified`). Sources are read through the
GitHub API at a specific commit, never from rendered pages. The commit
recorded for an entry is the one that last changed its source file, so it
only moves when upstream edits the statement.

When upstream only ever names its current release, an extractor may also read
the source's git history and return the rows upstream has since removed. Each
such row carries its last known values, a permalink to the last commit that
listed it, and `delisted`: the date of the commit that removed it.

An extractor raises UpstreamChanged when the statement no longer looks the way
it did when the parser was written. That is a prompt for a human to re-read
the source, not something to work around.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

from common import version_key

API = "https://api.github.com/"
EXTRACTORS = {}
_cache = {}


class UpstreamChanged(Exception):
    """The upstream statement could not be read the way the parser expects."""


def extractor(name):
    def register(func):
        EXTRACTORS[name] = func
        return func
    return register


# --- GitHub access -----------------------------------------------------------

def api(path, accept="application/vnd.github+json"):
    """GET a GitHub API path. Returns parsed JSON, or text for raw/sha media types."""
    key = (path, accept)
    if key not in _cache:
        request = urllib.request.Request(API + path, headers={"Accept": accept, "User-Agent": "k8s-m8s-verify"})
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        body = _fetch(request)
        _cache[key] = json.loads(body) if accept.endswith("+json") else body
    return _cache[key]


def _fetch(request):
    """Read a response body, retrying once on a network error or a server error."""
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            if err.code < 500 or attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(5)


def paged(path):
    items, page = [], 1
    while True:
        batch = api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def commit_sha(repo, ref="HEAD"):
    return api(f"repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}", "application/vnd.github.sha").strip()


def file_commit(repo, sha, path):
    """The most recent commit, at or before sha, that changed the file at path."""
    history = api(f"repos/{repo}/commits?path={urllib.parse.quote(path)}&sha={sha}&per_page=1")
    need(history, f"no commit history for {path} in {repo}")
    return history[0]["sha"]


def read(repo, sha, path):
    return api(f"repos/{repo}/contents/{urllib.parse.quote(path)}?ref={sha}", "application/vnd.github.raw")


def listdir(repo, sha, path):
    return [item["name"] for item in api(f"repos/{repo}/contents/{urllib.parse.quote(path)}?ref={sha}")]


def has_tag(repo, tag):
    try:
        api(f"repos/{repo}/git/ref/tags/{tag}")
        return True
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return False
        raise


def released_branches(repo, pattern, tag_format, oldest):
    """(version, sha) for release branches at or after `oldest` whose .0 tag exists."""
    found = []
    for branch in paged(f"repos/{repo}/branches"):
        match = re.fullmatch(pattern, branch["name"])
        if not match or version_key(match.group(1)) < version_key(oldest):
            continue
        if has_tag(repo, tag_format.format(match.group(1))):
            found.append((match.group(1), branch["commit"]["sha"]))
    return sorted(found, key=lambda item: version_key(item[0]), reverse=True)


def stable_releases(repo):
    return [r for r in paged(f"repos/{repo}/releases") if not r["prerelease"] and not r["draft"]]


# --- parsing helpers ---------------------------------------------------------

def table_rows(text):
    """Cells of every Markdown table row in text, in order."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def minors(text):
    return sorted(set(re.findall(r"(?<![\d.])1\.\d+(?![\d.])", text)), key=version_key)


def minor_range(low, high):
    return [f"1.{n}" for n in range(int(low.split(".")[1]), int(high.split(".")[1]) + 1)]


def contiguous(versions):
    return versions == minor_range(versions[0], versions[-1])


def need(condition, message):
    if not condition:
        raise UpstreamChanged(message)


def plain(cell):
    """Strip Markdown link, emphasis and code markup from a table cell."""
    cell = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", cell)
    cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)
    return re.sub(r"[*`]|<[^>]+>", "", cell).strip()


# --- backup and storage ------------------------------------------------------

@extractor("velero")
def velero():
    repo, path = "velero-io/velero", "README.md"
    sha = commit_sha(repo)
    rows = table_rows(read(repo, sha, path))
    header = next((r for r in rows if r[0] == "Velero version"), None)
    need(header and len(header) == 3 and header[2].startswith("Tested on"), "README table header changed")
    releases = []
    for row in rows:
        if len(row) != 3 or not re.fullmatch(r"\d+\.\d+", row[0]):
            continue
        expected = re.fullmatch(r"(1\.\d+)-(latest|1\.\d+)", row[1])
        need(expected, f"unexpected range '{row[1]}' for {row[0]}")
        releases.append({
            "version": row[0],
            "tested": re.findall(r"1\.\d+\.\d+", row[2]),
            "stated": {"min": expected.group(1), "max": expected.group(2), "wording": header[1]},
            "source": f"https://github.com/{repo}#velero-compatibility-matrix",
            "source_commit": file_commit(repo, sha, path),
        })
    return releases


@extractor("rook")
def rook():
    repo = "rook/rook"
    path = "Documentation/Getting-Started/Prerequisites/prerequisites.md"
    releases = []
    for version, sha in released_branches(repo, r"release-(1\.\d+)", "v{}.0", "1.15"):
        match = re.search(
            r"Kubernetes versions \*\*v(1\.\d+)\*\* through \*\*v(1\.\d+)\*\* (are supported)",
            read(repo, sha, path),
        )
        need(match, f"supported-versions sentence not found on release-{version}")
        releases.append({
            "version": version,
            "stated": {"min": match.group(1), "max": match.group(2), "wording": match.group(3)},
            "source": f"https://github.com/{repo}/blob/release-{version}/{path}",
            "source_commit": file_commit(repo, sha, path),
        })
    return releases


@extractor("longhorn")
def longhorn():
    repo, docs = "longhorn/website", "content/docs"
    sha = commit_sha(repo)
    released = {r["tag_name"].lstrip("v") for r in stable_releases("longhorn/longhorn")}
    folders = [(name, f"{docs}/{name}") for name in listdir(repo, sha, docs)]
    folders += [(name, f"{docs}/archives/{name}") for name in listdir(repo, sha, f"{docs}/archives")]
    releases = []
    for version, folder in folders:
        if version not in released:
            continue
        try:
            path = f"{folder}/best-practices.md"
            text = read(repo, sha, path)
        except urllib.error.HTTPError:
            continue
        _, phrase, after = text.partition("have been tested with Longhorn v{{< current-version >}}")
        if not phrase:
            continue
        tested = []
        for line in after.splitlines()[1:]:
            if line.startswith(("#", "Referenced")):
                break
            match = re.match(r"\|\s*(1\.\d+)\s*\|", line)
            if match:
                tested.append(match.group(1))
        need(tested, f"no tested versions listed for {version}")
        releases.append({
            "version": version,
            "tested": tested,
            "source": f"https://github.com/{repo}/blob/master/{path}",
            "source_commit": file_commit(repo, sha, path),
        })
    return releases


# --- networking --------------------------------------------------------------

@extractor("cloudnative-pg")
def cloudnative_pg():
    """Both release tables' "Supported Kubernetes versions" column. The current table's
    "Tested, but not supported" column is left out (see the tool's notes)."""
    repo, path = "cloudnative-pg/cloudnative-pg", "docs/src/supported_releases.md"
    sha = commit_sha(repo)
    text = read(repo, sha, path)
    wording = "Supported Kubernetes versions"
    commit = file_commit(repo, sha, path)
    releases, header, tables = [], None, 0
    for row in table_rows(text):
        if row[0] == "Version":
            header = row if wording in row else None
            tables += header is not None
            continue
        if not header or set(row[0]) <= set("-"):
            continue
        need(len(row) == len(header), f"unexpected row {row[:2]}")
        if row[0] == "main":
            continue  # development branch, no release
        version = re.fullmatch(r"(\d+\.\d+)\.x", row[0])
        minors_listed = [m.strip() for m in row[header.index(wording)].split(",")]
        need(version and all(re.fullmatch(r"1\.\d+", m) for m in minors_listed), f"unexpected row {row[:2]}")
        supported = sorted(minors_listed, key=version_key)
        need(contiguous(supported), f"supported versions for {row[0]} are not one range")
        releases.append({
            "version": version.group(1),
            "stated": {"min": supported[0], "max": supported[-1], "wording": wording},
            "source": f"https://github.com/{repo}/blob/main/{path}",
            "source_commit": commit,
        })
    need(tables == 2, f"expected two tables with a '{wording}' column, found {tables}")
    return releases


@extractor("cilium")
def cilium():
    repo = "cilium/cilium"
    path = "Documentation/network/kubernetes/compatibility.rst"
    releases = []
    for version, sha in released_branches(repo, r"v(1\.\d+)", "v{}.0", "1.15"):
        text = read(repo, sha, path)
        need("All Kubernetes versions listed are e2e tested" in text, f"testing sentence changed on v{version}")
        row = re.search(r"^\|\s*((?:1\.\d+,?\s*)+)\|", text, re.MULTILINE)
        need(row, f"version row not found on v{version}")
        releases.append({
            "version": version,
            "tested": minors(row.group(1)),
            "source": f"https://github.com/{repo}/blob/v{version}/{path}",
            "source_commit": file_commit(repo, sha, path),
        })
    return releases


@extractor("contour")
def contour():
    repo, path = "projectcontour/contour", "site/content/resources/compatibility-matrix.md"
    sha = commit_sha(repo)
    rows = table_rows(read(repo, sha, path))
    need(any(len(r) > 2 and r[2] == "Tested Kubernetes Versions" for r in rows), "table header changed")
    return [
        {
            "version": row[0],
            "tested": minors(row[2]),
            "source": f"https://github.com/{repo}/blob/main/{path}",
            "source_commit": file_commit(repo, sha, path),
        }
        for row in rows
        if len(row) > 2 and re.fullmatch(r"\d+\.\d+\.\d+", row[0])
    ]


@extractor("envoy-gateway")
def envoy_gateway():
    repo, path = "envoyproxy/gateway", "site/content/en/news/releases/matrix.md"
    sha = commit_sha(repo)
    text = read(repo, sha, path)
    need("the Kubernetes column, which lists a range of tested versions" in text, "testing sentence changed")
    return [
        {
            "version": row[0][1:],
            "tested": minors(row[4]),
            "source": f"https://github.com/{repo}/blob/main/{path}",
            "source_commit": file_commit(repo, sha, path),
        }
        for row in table_rows(text)
        if len(row) > 4 and re.fullmatch(r"v\d+\.\d+", row[0])
    ]


@extractor("nginx-gateway-fabric")
def nginx_gateway_fabric():
    """The README's technical specifications table: one row per minor, naming its latest patch."""
    repo, path = "nginx/nginx-gateway-fabric", "README.md"
    sha = commit_sha(repo)
    text = read(repo, sha, path)
    section = re.search(r"(?ms)^## Technical Specifications\n(.*?)(?=^#{2,3} )", text)
    need(section, "technical specifications section not found")
    wording = "software versions NGINX Gateway Fabric supports"
    need(f"lists the {wording}. Only the latest patch release for each minor version is shown." in section.group(1),
         "the sentence introducing the table changed")
    rows = table_rows(section.group(1))
    need(rows and rows[0][:3] == ["NGINX Gateway Fabric", "Gateway API", "Kubernetes"], "table header changed")
    commit = file_commit(repo, sha, path)
    releases = []
    for row in rows[2:]:
        if row[0] == "Edge":
            continue  # unreleased main branch
        version = re.fullmatch(r"\d+\.\d+\.\d+", row[0])
        minimum = re.fullmatch(r"(1\.\d+)\+", row[2])
        need(version and minimum, f"unexpected row {row[:3]}")
        releases.append({
            "version": row[0],
            "stated": {"min": minimum.group(1), "max": "latest", "wording": wording},
            "source": f"https://github.com/{repo}#technical-specifications",
            "source_commit": commit,
        })
    return releases


@extractor("external-dns")
def external_dns():
    repo, path = "kubernetes-sigs/external-dns", "README.md"
    sha = commit_sha(repo)
    text = read(repo, sha, path)
    wording = "Kubernetes version compatibility"
    need(f"## {wording}" in text, "section heading changed")
    rows = table_rows(text)
    start = next((i for i, r in enumerate(rows) if r[0] == "ExternalDNS"), None)
    need(start is not None, "table not found")
    header = rows[start]
    body = [r for r in rows[start + 2:start + 12] if r[0].startswith("Kubernetes")]

    def bounds(cell):
        low = re.search(r"≥ (\d+\.\d+)", cell)
        high = re.search(r"≤ (\d+\.\d+)", cell)
        listed = re.findall(r"(?<![≥≤] )(?<![\d.])(\d+\.\d+)", cell) if not (low or high) else []
        return (low.group(1) if low else (listed[0] if listed else None),
                high.group(1) if high else (listed[-1] if listed else None))

    releases = []
    for column in range(1, len(header)):
        band_low, band_high = bounds(header[column])
        runs = []  # consecutive Kubernetes rows with the same mark, merged
        for row in body:
            mark = {":white_check_mark:": True, ":x:": False}.get(row[column])
            need(mark is not None, f"unexpected mark '{row[column]}'")
            low, high = bounds(row[0])
            if runs and runs[-1][0] == mark:
                runs[-1][2] = high
            else:
                runs.append([mark, low, high])
        compatible = [run for run in runs if run[0]]
        need(len(compatible) == 1, f"expected one compatible range for column '{header[column]}'")

        def k8s_range(run):
            result = {"min": run[1]} if run[1] else {}
            result["max"] = run[2] or "latest"
            return result

        band = {}
        if band_low:
            band["min"] = band_low
        if band_high:
            band["max"] = band_high
        releases.append({
            "version_range": band,
            "stated": {**k8s_range(compatible[0]), "wording": wording},
            "unsupported": [k8s_range(run) for run in runs if not run[0]],
            "source": f"https://github.com/{repo}#kubernetes-version-compatibility",
            "source_commit": file_commit(repo, sha, path),
        })
    return releases


@extractor("istio")
def istio():
    repo, path = "istio/istio.io", "data/compatibility/supportStatus.yml"
    sha = commit_sha(repo)
    wording = "Supported Kubernetes Versions"
    need(wording in read(repo, sha, "layouts/shortcodes/support_status_table.html"), "column heading changed")
    releases = []
    for entry in yaml.safe_load(read(repo, sha, path)):
        version, supported = str(entry["version"]), entry.get("k8sVersions") or []
        if not re.fullmatch(r"\d+\.\d+", version) or not supported:
            continue
        supported = sorted(supported, key=version_key)
        need(contiguous(supported), f"supported versions for {version} are not one range")
        releases.append({
            "version": version,
            "stated": {"min": supported[0], "max": supported[-1], "wording": wording},
            "source": f"https://github.com/{repo}/blob/master/{path}",
            "source_commit": file_commit(repo, sha, path),
        })
    return releases


@extractor("linkerd")
def linkerd():
    """The current docs' table of minimum and maximum Kubernetes versions per Linkerd version."""
    repo, path = "linkerd/website", "linkerd.io/content/docs/reference/k8s-versions.md"
    sha = commit_sha(repo)
    text = read(repo, sha, path)
    title = re.search(r"(?m)^title: (.+)$", text)
    need(title and title.group(1).strip() == "Supported Kubernetes Versions", "page title changed")
    rows = [[plain(cell) for cell in row] for row in table_rows(text)]
    need(rows and rows[0] == ["Linkerd Version", "Minimum Kubernetes Version", "Maximum Kubernetes Version"],
         "table header changed")
    commit = file_commit(repo, sha, path)
    releases = []
    for row in rows[2:]:
        need(len(row) == 3 and re.fullmatch(r"2\.\d+", row[0]) and all(re.fullmatch(r"1\.\d+", c) for c in row[1:]),
             f"unexpected row {row}")
        need(version_key(row[1]) <= version_key(row[2]), f"range for {row[0]} runs backwards")
        releases.append({
            "version": row[0],
            "stated": {"min": row[1], "max": row[2], "wording": title.group(1).strip()},
            "source": f"https://github.com/{repo}/blob/main/{path}",
            "source_commit": commit,
        })
    return releases


@extractor("calico")
def calico():
    repo = "tigera/docs"
    sha = commit_sha(repo)
    releases = []
    for folder in listdir(repo, sha, "calico_versioned_docs"):
        version = re.fullmatch(r"version-(\d+\.\d+)", folder)
        if not version or not has_tag("projectcalico/calico", f"v{version.group(1)}.0"):
            continue
        path = f"calico_versioned_docs/{folder}/_includes/partials/_system-requirements.mdx"
        _, phrase, after = read(repo, sha, path).partition(
            "We test $[prodname] $[version] against the following Kubernetes versions."
        )
        need(phrase, f"testing sentence not found for {version.group(1)}")
        tested = []
        for line in after.splitlines()[1:]:
            item = re.fullmatch(r"\* (1\.\d+)", line.strip())
            if item:
                tested.append(item.group(1))
            elif tested:
                break
        need(tested, f"no tested versions listed for {version.group(1)}")
        entry = {"version": version.group(1), "tested": tested}
        too_old = re.search(r"will not work\s+on Kubernetes v(1\.\d+) or below", after)
        if too_old:
            entry["unsupported"] = [{"max": too_old.group(1)}]
        releases.append({**entry, "source": f"https://github.com/{repo}/blob/main/{path}", "source_commit": file_commit(repo, sha, path)})
    return releases


# --- GitOps and delivery -----------------------------------------------------

@extractor("helm")
def helm():
    """Helm 4 rows from the current docs, Helm 3 and 2 rows from the version-3 docs."""
    repo = "helm/helm-www"
    sha = commit_sha(repo)
    releases = []
    for path in ("docs/topics/version_skew.mdx", "versioned_docs/version-3/topics/version_skew.md"):
        rows = table_rows(read(repo, sha, path))
        need(rows and rows[0] == ["Helm Version", "Supported Kubernetes Versions"], f"table header changed in {path}")
        commit = file_commit(repo, sha, path)
        for row in rows[2:]:
            version = re.fullmatch(r"(\d+\.\d+)\.x", row[0])
            supported = re.fullmatch(r"(1\.\d+)\.x - (1\.\d+)\.x", row[1]) if len(row) == 2 else None
            need(version and supported, f"unexpected row {row} in {path}")
            high, low = supported.groups()
            need(version_key(low) <= version_key(high), f"range for {version.group(1)} runs backwards")
            releases.append({
                "version": version.group(1),
                "stated": {"min": low, "max": high, "wording": rows[0][1]},
                "source": f"https://github.com/{repo}/blob/main/{path}",
                "source_commit": commit,
            })
    versions = [r["version"] for r in releases]
    need(len(versions) == len(set(versions)), "a Helm version is listed twice")
    return releases


@extractor("argo-cd")
def argo_cd():
    repo, path = "argoproj/argo-cd", "docs/operator-manual/tested-kubernetes-versions.md"
    releases = []
    for version, sha in released_branches(repo, r"release-(\d+\.\d+)", "v{}.0", "3.0"):
        row = next((r for r in table_rows(read(repo, sha, path)) if r[0] == version), None)
        need(row and len(row) == 2, f"no row for {version} on its release branch")
        releases.append({
            "version": version,
            "tested": minors(row[1]),
            "source": f"https://github.com/{repo}/blob/release-{version}/{path}",
            "source_commit": file_commit(repo, sha, path),
        })
    return releases


@extractor("flux")
def flux():
    repo = "fluxcd/flux2"
    wording = "compatible with the following Kubernetes versions"
    releases = []
    for release in stable_releases(repo):
        tag = re.fullmatch(r"v(2\.\d+)\.0", release["tag_name"])
        if not tag:
            continue
        _, phrase, after = (release["body"] or "").partition(wording)
        need(phrase, f"compatibility statement not found in {release['tag_name']} release notes")
        listed = []
        for row in table_rows(after):
            if re.fullmatch(r"`v1\.\d+`.*", row[0]):
                listed.append(row[0])
            elif listed:
                break
        versions = minors(" ".join(listed))
        need(versions and contiguous(versions), f"unexpected version list in {release['tag_name']}")
        releases.append({
            "version": tag.group(1),
            "stated": {
                "min": versions[0],
                "max": "latest" if any("later" in cell for cell in listed) else versions[-1],
                "wording": wording,
            },
            "source": f"https://github.com/{repo}/releases/tag/{release['tag_name']}",
            "source_commit": commit_sha(repo, release["tag_name"]),
        })
    return releases


# --- security ----------------------------------------------------------------

@extractor("cert-manager")
def cert_manager():
    repo, path = "cert-manager/website", "content/docs/releases/README.md"
    sha = commit_sha(repo)
    source = f"https://github.com/{repo}/blob/master/{path}"
    releases, header = [], []
    for row in table_rows(read(repo, sha, path)):
        if row[0] == "Release":
            header = [plain(cell) for cell in row]
            continue
        version = re.fullmatch(r"(\d+\.\d+)( LTS)?", plain(row[0]))
        if not version or len(row) != len(header) or len(header) != 5:
            continue
        entry = {"version": version.group(1)}
        stated = re.match(r"(1\.\d+) → (1\.\d+)", row[3])
        if header[4] == "Tested Kubernetes Versions":
            tested = re.fullmatch(r"(1\.\d+) → (1\.\d+)", row[4])
            if not stated and not tested:
                continue  # upcoming release, still TBD
            need(stated and tested, f"unexpected ranges for {version.group(1)}")
            entry["tested"] = minor_range(*tested.groups())
        elif header[3] != "Compatible Kubernetes versions":
            continue
        need(stated, f"unexpected range for {version.group(1)}")
        entry["stated"] = {"min": stated.group(1), "max": stated.group(2), "wording": header[3]}
        releases.append({**entry, "source": source, "source_commit": file_commit(repo, sha, path)})
    return releases


@extractor("external-secrets")
def external_secrets():
    repo, path = "external-secrets/external-secrets", "docs/introduction/stability-support.md"
    sha = commit_sha(repo)
    rows = table_rows(read(repo, sha, path))
    start = next((i for i, r in enumerate(rows) if r[0] == "ESO Version"), None)
    need(start is not None and rows[start][1] == "Kubernetes Version", "support table header changed")
    releases = []
    for row in rows[start + 2:]:
        version = re.fullmatch(r"(\d+\.\d+(?:\.\d+)?)(\.x)?", row[0])
        if not version:
            break
        span = re.fullmatch(r"(1\.\d+)(?:\s*(?:-|→)\s*(1\.\d+))?", row[1])
        need(span, f"unexpected Kubernetes version '{row[1]}' for {row[0]}")
        releases.append({
            "version": version.group(1),
            "stated": {"min": span.group(1), "max": span.group(2) or span.group(1), "wording": rows[start][1]},
            "source": f"https://github.com/{repo}/blob/main/{path}",
            "source_commit": file_commit(repo, sha, path),
        })
    return releases


# --- observability, policy and scaling ---------------------------------------

@extractor("keda")
def keda():
    repo = "kedacore/keda-docs"
    sha = commit_sha(repo)
    folders = [name for name in listdir(repo, sha, "content/docs") if re.fullmatch(r"\d+\.\d+", name)]
    # the newest documentation version whose own row is filled in
    for folder in sorted(folders, key=version_key, reverse=True):
        path = f"content/docs/{folder}/operate/cluster.md"
        text = read(repo, sha, path)
        rows = [r for r in table_rows(text) if len(r) == 2 and re.fullmatch(r"v\d+\.\d+", r[0])]
        own = next((r for r in rows if r[0] == f"v{folder}"), None)
        if own and re.fullmatch(r"v1\.\d+ - v1\.\d+", own[1]):
            break
    else:
        raise UpstreamChanged("no documentation version with a filled-in row")
    need("shows the Kubernetes versions tested for each KEDA version" in text, "testing sentence changed")
    releases = []
    for row in rows:
        span = re.fullmatch(r"v(1\.\d+) - v(1\.\d+)", row[1])
        if not span:
            continue  # TBD
        releases.append({
            "version": row[0][1:],
            "tested": minor_range(*span.groups()),
            "source": f"https://github.com/{repo}/blob/main/{path}",
            "source_commit": file_commit(repo, sha, path),
        })
    return releases


KYVERNO_WORDING = "Kubernetes Versions Supported"


def kyverno_statement(text):
    """(version, min, max) from one revision of the releases page, or None if it has no statement."""
    version = re.search(r"\*\*(?:Supported|Latest) Release:\*\*\s*\|\s*v(\d+\.\d+)", text)
    span = re.search(rf"\*\*{KYVERNO_WORDING}:\*\*\s*\|\s*v(1\.\d+) - v(1\.\d+)", text)
    if not version and not span:
        return None
    need(version and span, "supported release table changed")
    return version.group(1), span.group(1), span.group(2)


@extractor("opentelemetry-operator")
def opentelemetry_operator():
    """The compatibility matrix: a supported Kubernetes range for each operator release."""
    repo, path = "open-telemetry/opentelemetry-operator", "docs/getting-started/compatibility.md"
    sha = commit_sha(repo)
    text = read(repo, sha, path)
    wording = "supported Kubernetes versions"
    need(f"precisely shows the {wording} for each operator release" in text, "sentence introducing the matrix changed")
    rows = table_rows(text)
    start = next((i for i, r in enumerate(rows) if r[:2] == ["OpenTelemetry Operator", "Kubernetes"]), None)
    need(start is not None, "compatibility matrix not found")
    commit = file_commit(repo, sha, path)
    releases = []
    for row in rows[start + 2:]:
        need(len(row) == 4, f"unexpected row {row}")
        if row[1] == "N/A":
            continue  # upstream makes no claim for this release
        version = re.fullmatch(r"v(\d+\.\d+\.\d+)", row[0])
        supported = re.fullmatch(r"v(1\.\d+) to v(1\.\d+)", row[1])
        need(version and supported, f"unexpected row {row[:2]}")
        low, high = supported.groups()
        need(version_key(low) <= version_key(high), f"range for {row[0]} runs backwards")
        releases.append({
            "version": version.group(1),
            "stated": {"min": low, "max": high, "wording": wording},
            "source": f"https://github.com/{repo}/blob/main/{path}#compatibility-matrix",
            "source_commit": commit,
        })
    return releases


@extractor("kyverno")
def kyverno():
    # The page names only the one supported release, so earlier releases are
    # read from its git history: each from the last commit that named it, and
    # delisted on the date of the commit that replaced it.
    repo, path = "kyverno/website", "src/content/docs/docs/installation/releases.md"
    sha = commit_sha(repo)
    history = paged(f"repos/{repo}/commits?path={urllib.parse.quote(path)}&sha={sha}")  # newest first
    need(history, f"no commit history for {path} in {repo}")
    releases, seen, newer = [], set(), None
    for commit in history:
        statement = kyverno_statement(read(repo, commit["sha"], path))
        if newer is None:
            need(statement, "supported release table not found")
        if statement and statement[0] not in seen:
            version, low, high = statement
            seen.add(version)
            entry = {
                "version": version,
                "stated": {"min": low, "max": high, "wording": KYVERNO_WORDING},
                "source": f"https://github.com/{repo}/blob/{'main' if newer is None else commit['sha']}/{path}",
                "source_commit": commit["sha"],
            }
            if newer is not None:
                entry["delisted"] = newer["commit"]["committer"]["date"][:10]
            releases.append(entry)
        newer = commit
    return releases


@extractor("metrics-server")
def metrics_server():
    repo, path = "kubernetes-sigs/metrics-server", "README.md"
    sha = commit_sha(repo)
    # this table is written without leading pipes
    rows = [[cell.strip() for cell in line.split("|")] for line in read(repo, sha, path).splitlines() if "|" in line]
    header = next((r for r in rows if r[0] == "Metrics Server"), None)
    need(header and len(header) == 3, "compatibility matrix header changed")
    releases = []
    for row in rows:
        version = re.fullmatch(r"(\d+\.\d+)\.x", row[0])
        if not version or len(row) != 3:
            continue
        span = re.fullmatch(r"\*?(1\.\d+)(?:\+|-(1\.\d+))", row[2])
        need(span, f"unexpected range '{row[2]}' for {row[0]}")
        releases.append({
            "version": version.group(1),
            "stated": {"min": span.group(1), "max": span.group(2) or "latest", "wording": header[2]},
            "source": f"https://github.com/{repo}#compatibility-matrix",
            "source_commit": file_commit(repo, sha, path),
        })
    return releases


@extractor("kube-prometheus")
def kube_prometheus():
    """The README's compatibility grid, per release branch. A tick is recorded as stated;
    an x is not recorded, because upstream does not say those versions do not work."""
    repo, path = "prometheus-operator/kube-prometheus", "README.md"
    sha = commit_sha(repo)
    text = read(repo, sha, path)
    section = re.search(r"(?ms)^## Compatibility\n(.*?)(?=^## )", text)
    need(section, "compatibility section not found")
    need("The following Kubernetes versions are supported and work as we test against these versions in their "
         "respective branches." in section.group(1), "the sentence introducing the grid changed")
    rows = table_rows(section.group(1))
    need(rows and rows[0][0] == "kube-prometheus stack", "table header changed")
    columns = []
    for cell in rows[0][1:]:
        minor = re.fullmatch(r"Kubernetes (1\.\d+)", cell)
        need(minor, f"unexpected column '{cell}'")
        columns.append(minor.group(1))
    commit = file_commit(repo, sha, path)
    releases = []
    for row in rows[2:]:
        branch = plain(row[0])
        if branch == "main":
            continue  # development branch, no release
        version = re.fullmatch(r"release-(\d+\.\d+)", branch)
        need(version and len(row) == len(columns) + 1 and all(c in ("✔", "x") for c in row[1:]),
             f"unexpected row {row[:2]}")
        supported = [m for m, c in zip(columns, row[1:]) if c == "✔"]
        if not supported:
            continue
        need(contiguous(supported), f"supported versions for {branch} are not one range")
        releases.append({
            "version": version.group(1),
            "stated": {"min": supported[0], "max": supported[-1], "wording": "Compatibility"},
            "source": f"https://github.com/{repo}#compatibility",
            "source_commit": commit,
        })
    return releases


@extractor("cluster-autoscaler")
def cluster_autoscaler():
    repo, path = "kubernetes/autoscaler", "cluster-autoscaler/README.md"
    sha = commit_sha(repo)
    text = read(repo, sha, path)
    need("The below combinations have been tested on GCP" in text, "testing sentence changed")
    tested = {}
    for row in table_rows(text):
        kubernetes = re.fullmatch(r"(\d+\.\d+)\.[xX]", row[0])
        if not kubernetes or len(row) < 2:
            continue
        for item in plain(row[1]).split(","):
            version = re.fullmatch(r"(\d+\.\d+)\.[xX]", item.strip())
            need(version, f"unexpected Cluster Autoscaler version '{item}'")
            tested.setdefault(version.group(1), []).append(kubernetes.group(1))
    return [
        {
            "version": version,
            "tested": sorted(kubernetes, key=version_key),
            "source": f"https://github.com/{repo}/blob/master/{path}#releases",
            "source_commit": file_commit(repo, sha, path),
        }
        for version, kubernetes in tested.items()
    ]


@extractor("karpenter")
def karpenter():
    """Transposes upstream's table (minimum Karpenter version per Kubernetes minor)."""
    repo, path = "aws/karpenter-provider-aws", "website/content/en/docs/upgrading/compatibility.md"
    sha = commit_sha(repo)
    text = read(repo, sha, path)
    wording = "Compatibility Matrix"
    need(f"## {wording}" in text, "section heading changed")
    rows = table_rows(text)
    start = next((i for i, r in enumerate(rows) if r[0] == "Kubernetes"), None)
    need(start is not None and rows[start + 2][0] == "karpenter", "matrix not found")
    columns = list(zip(rows[start][1:], rows[start + 2][1:]))
    need(contiguous([k for k, _ in columns]), "Kubernetes columns are not one range")
    minimums = []  # (Kubernetes minor, lowest Karpenter version)
    for index, (kubernetes, cell) in enumerate(columns):
        at_least = re.fullmatch(r"\\?>= (\d+\.\d+(?:\.\d+)?)", cell)
        only = re.fullmatch(r"(\d+\.\d+)\.x", cell)
        need(at_least or (only and index == len(columns) - 1), f"unexpected cell '{cell}'")
        minimums.append((kubernetes, (at_least or only).group(1)))
    starts = sorted({version for _, version in minimums}, key=version_key)
    releases = []
    for index, first in enumerate(starts):
        covered = [k for k, version in minimums if version_key(version) <= version_key(first)]
        if index + 1 < len(starts):
            following = version_key(starts[index + 1])
            last = f"{following[0]}.{following[1] - 1}"
        else:
            last = first  # stop at the highest version upstream names
        entry = {"version": first} if first == last else {"version_range": {"min": first, "max": last}}
        releases.append({
            **entry,
            "stated": {"min": covered[0], "max": covered[-1], "wording": wording},
            "source": f"https://github.com/{repo}/blob/main/{path}",
            "source_commit": file_commit(repo, sha, path),
        })
    return releases


# --- workloads and runtimes --------------------------------------------------

@extractor("cluster-api")
def cluster_api():
    """The core provider's support matrix (rows: Kubernetes minors, columns: Cluster API
    minors). Only plain ticks are recorded: "(only workload)" cells and cells that apply
    from a given patch release on ("✓ >= v1.13.1") are left out (see the tool's notes)."""
    repo, path = "kubernetes-sigs/cluster-api", "docs/book/src/reference/versions.md"
    sha = commit_sha(repo)
    text = read(repo, sha, path)
    section = re.search(r"(?ms)^### Core provider \(`cluster-api-controller`\)\n(.*?)(?=^### )", text)
    need(section, "core provider section not found")
    wording = "support matrix"
    need(f"defines the {wording} for the Cluster API core provider" in section.group(1),
         "the sentence introducing the matrix changed")
    rows = table_rows(section.group(1))
    need(rows and rows[0][0] == "", "matrix header changed")
    versions = []
    for cell in rows[0][1:]:
        version = re.fullmatch(r"v(\d+\.\d+)(, _Maintenance Mode_)?", cell)
        need(version, f"unexpected column '{cell}'")
        versions.append(version.group(1))
    supported = {v: [] for v in versions}
    for row in rows[2:]:
        minor = re.fullmatch(r"Kubernetes v(1\.\d+)", row[0])
        need(minor and len(row) == len(versions) + 1, f"unexpected row {row[:2]}")
        for version, cell in zip(versions, row[1:]):
            need(re.fullmatch(r"|✓|✓ \(only workload\)|✓ >= v\d+\.\d+\.\d+", cell), f"unexpected cell '{cell}'")
            if cell == "✓":
                supported[version].append(minor.group(1))
    commit = file_commit(repo, sha, path)
    releases = []
    for version, minors_ in supported.items():
        if not minors_:
            continue
        minors_ = sorted(minors_, key=version_key)
        need(contiguous(minors_), f"supported versions for {version} are not one range")
        releases.append({
            "version": version,
            "stated": {"min": minors_[0], "max": minors_[-1], "wording": wording},
            "source": f"https://github.com/{repo}/blob/main/{path}#core-provider-cluster-api-controller",
            "source_commit": commit,
        })
    return releases


@extractor("kubevirt")
def kubevirt():
    """The sig-release support matrix. Upstream defines an EOL cell as a Kubernetes
    version that KubeVirt supported, now past its end of life, so it is a support
    claim like a tick."""
    repo, path = "kubevirt/sig-release", "releases/k8s-support-matrix.md"
    sha = commit_sha(repo)
    text = read(repo, sha, path)
    wording = "KubeVirt to Kubernetes version support matrix"
    need(text.lstrip().startswith(f"# {wording}"), "page title changed")
    need("_EOL_ means that the Kubernetes version was supported by KubeVirt but has reached end of life" in text,
         "the note defining EOL changed")
    rows = table_rows(text)
    need(rows and rows[0][0] == "KubeVirt version", "table header changed")
    columns = []
    for cell in rows[0][1:]:
        minor = re.fullmatch(r"(1\.\d+)(<sup>EOL</sup>)?", cell)
        need(minor, f"unexpected column '{cell}'")
        columns.append(minor.group(1))
    commit = file_commit(repo, sha, path)
    releases = []
    for row in rows[2:]:
        need(len(row) == len(columns) + 1 and re.fullmatch(r"\d+\.\d+", row[0]), f"unexpected row {row[:2]}")
        need(all(c in ("✓", "EOL", "-") for c in row[1:]), f"unexpected cell in row {row[0]}")
        supported = sorted((m for m, c in zip(columns, row[1:]) if c in ("✓", "EOL")), key=version_key)
        if not supported:
            continue
        need(contiguous(supported), f"supported versions for {row[0]} are not one range")
        releases.append({
            "version": row[0],
            "stated": {"min": supported[0], "max": supported[-1], "wording": wording},
            "source": f"https://github.com/{repo}/blob/main/{path}",
            "source_commit": commit,
        })
    return releases


# --- Kubernetes window -------------------------------------------------------

def kubernetes_window():
    """(latest three Kubernetes minors, commit) from the release schedule data."""
    repo = "kubernetes/website"
    sha = commit_sha(repo)
    window = window_from_schedule(read(repo, sha, "data/releases/schedule.yaml"))
    return window, file_commit(repo, sha, "data/releases/schedule.yaml")


def window_from_schedule(text):
    """The latest three released minors listed in the release schedule data."""
    schedule = yaml.safe_load(text)
    released = sorted((str(s["release"]) for s in schedule["schedules"]), key=version_key, reverse=True)
    need(len(released) >= 3, "release schedule lists fewer than three minors")
    return released[:3]
