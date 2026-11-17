"""Minimal GitHub REST client for the automation scripts (standard library only).

The token is read from GH_TOKEN for each call, so a script can hold a
read-only token for its checks and is given a write token only in the
workflow step that needs one. The repository is GITHUB_REPOSITORY.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com/"


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def repo():
    name = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in name:
        raise SystemExit("GITHUB_REPOSITORY is not set")
    return name


def call(method, path, body=None, accept="application/vnd.github+json", retry=True):
    """One API call. Paths are relative to https://api.github.com/. Retries once on a server error."""
    url = path if path.startswith("https://") else API + path.lstrip("/")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": accept,
        "User-Agent": "k8s-m8s-automation",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if data is not None:
        request.add_header("Content-Type", "application/json")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:500]
        if err.code >= 500 and retry:
            time.sleep(5)
            return call(method, path, body, accept, retry=False)
        raise GitHubError(err.code, detail) from None
    except urllib.error.URLError:
        if retry:
            time.sleep(5)
            return call(method, path, body, accept, retry=False)
        raise
    if not raw:
        return None
    return json.loads(raw) if accept.endswith("json") else raw


def get(path, **params):
    query = urllib.parse.urlencode(params)
    return call("GET", f"{path}?{query}" if query else path)


def paged(path, key=None, **params):
    """Every item of a paginated list. `key` names the list inside an object response."""
    items, page = [], 1
    while True:
        batch = get(path, per_page=100, page=page, **params)
        batch = batch[key] if key else batch
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def quote(value):
    return urllib.parse.quote(value, safe="")
