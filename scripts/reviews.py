"""The review comment that post_review.py writes and automerge.py reads.

A comment carries two hidden markers, both written by post_review.py:

    <!-- k8s-m8s-review-rows {"<finding id>": "<verdict>", ...} -->
    <!-- k8s-m8s-review head=<40-hex sha> verdict=<confirmed|rejected> -->

Everything Claude wrote (the reasons) is HTML-escaped before it goes into the
comment, so it cannot contain a marker. A comment with more than one of
either marker is not a valid review.
"""

import html
import json
import re

REVIEW = re.compile(r"<!-- k8s-m8s-review head=([0-9a-f]{40}) verdict=(confirmed|rejected) -->")
ROWS = re.compile(r"<!-- k8s-m8s-review-rows (\{[^<>]*\}) -->")
VERDICTS = ("confirmed", "mismatch", "ambiguous")


def escape(text):
    """Plain text that is safe inside a Markdown table cell and cannot form a marker."""
    text = html.escape(" ".join(str(text).split()), quote=True)
    return text.replace("|", "&#124;").replace("`", "&#96;").replace("\\", "&#92;")


def render(head, rows, problems, run_url):
    """The comment body. rows: [{"id", "verdict", "reason"}] already validated; problems force rejection."""
    confirmed = not problems and bool(rows) and all(r["verdict"] == "confirmed" for r in rows)
    verdict = "confirmed" if confirmed else "rejected"
    lines = [f"### Automated review of `{head[:7]}`: {verdict}", ""]
    if rows:
        lines += ["| Finding | Verdict | Reason |", "|---|---|---|"]
        lines += [f"| {escape(r['id'])} | {escape(r['verdict'])} | {escape(r['reason'])} |" for r in rows]
        lines.append("")
    if problems:
        lines.append("The review output was not accepted:")
        lines += [f"- {escape(p)}" for p in problems]
        lines.append("")
    lines.append("Each change was checked against the upstream file at the recorded commit. "
                 f"[Workflow run]({run_url}).")
    lines.append("")
    table = {r["id"]: r["verdict"] for r in rows} if not problems else {}
    rows_json = json.dumps(table, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
    lines.append(f"<!-- k8s-m8s-review-rows {rows_json} -->")
    lines.append(f"<!-- k8s-m8s-review head={head} verdict={verdict} -->")
    return "\n".join(lines) + "\n"


def parse(body):
    """(head, verdict, {id: verdict}) from a review comment. Raises ValueError if it is not exactly one review."""
    reviews, rows = REVIEW.findall(body or ""), ROWS.findall(body or "")
    if len(reviews) != 1 or len(rows) != 1:
        raise ValueError(f"expected one review marker and one rows marker, found {len(reviews)} and {len(rows)}")
    table = json.loads(rows[0])
    if not isinstance(table, dict) or not all(isinstance(v, str) for v in table.values()):
        raise ValueError("rows marker is not a map of verdicts")
    return reviews[0][0], reviews[0][1], table


def latest(comments, login):
    """The newest comment by the reviewer App (a Bot account with that login), or None."""
    mine = [c for c in comments if c["user"]["login"] == login and c["user"].get("type") == "Bot"]
    return max(mine, key=lambda c: (c["created_at"], c["id"])) if mine else None


def reviewed_heads(comments, login):
    """Head SHAs the reviewer App has posted a review for, from unedited comments."""
    heads = set()
    for c in comments:
        if c["user"]["login"] == login and c["user"].get("type") == "Bot" and c["created_at"] == c["updated_at"]:
            try:
                heads.add(parse(c["body"])[0])
            except ValueError:
                pass
    return heads
