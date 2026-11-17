"""The machine-readable findings block embedded in automated pull request bodies."""

import json
import re

MARKER = "<!-- k8s-m8s-findings -->"
_BLOCK = re.compile(re.escape(MARKER) + r"\s*```json\n(.*?)\n```", re.S)


def render(findings):
    return f"{MARKER}\n```json\n{json.dumps(findings, indent=2, sort_keys=True)}\n```\n"


def parse(body):
    """The findings list from a pull request body. Raises ValueError unless there is exactly one block."""
    blocks = _BLOCK.findall(body or "")
    if len(blocks) != 1:
        raise ValueError(f"expected one findings block, found {len(blocks)}")
    data = json.loads(blocks[0])
    if not isinstance(data, list) or not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in data):
        raise ValueError("findings block is not a list of findings with ids")
    return data


def comparable(findings):
    """Findings in a form that stays the same between daily runs while upstream does not change."""
    keep = ("id", "class", "fields", "source", "source_commit", "old", "new")
    return sorted((json.dumps({k: f.get(k) for k in keep}, sort_keys=True) for f in findings))
