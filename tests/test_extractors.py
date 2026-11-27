"""Unit tests for extractors that parse a statement's text, without reading upstream."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from extractors import UpstreamChanged, cloudnative_pg_rows  # noqa: E402

REPO, PATH, COMMIT = "cloudnative-pg/cloudnative-pg", "docs/src/supported_releases.md", "c" * 40

CURRENT = """
| Version | Currently supported  | Release date | End of life | Supported Kubernetes versions | Tested, but not supported | Supported Postgres versions |
|---------|----------------------|--------------|-------------|-------------------------------|---------------------------|-----------------------------|
| 1.31.x  | No, preview (RC)     | ~ Dec 2026   | ~ Jun 2027  | 1.35, 1.36, 1.37              | 1.34, 1.33                | 15 - 19                     |
| 1.30.x  | Yes                  | 29 Jun 2026  | ~ Mar 2027  | 1.34, 1.35, 1.36              | 1.37, 1.33                | 15 - 18                     |
| main    | No, development only |              |             |                               |                           | 15 - 19                     |
"""

OLD = """
| Version | Release date   | End of life        | Supported Kubernetes versions |
|---------|----------------|--------------------|-------------------------------|
| 1.29.x  | March 31, 2026 | September 29, 2026 | 1.33, 1.34, 1.35              |
"""


class CloudNativePGTest(unittest.TestCase):
    def rows(self, text):
        return {r["version"]: r["stated"] for r in cloudnative_pg_rows(text, REPO, PATH, COMMIT)}

    def test_release_candidates_and_the_development_branch_are_left_out(self):
        self.assertEqual(self.rows(CURRENT + OLD), {
            "1.30": {"min": "1.34", "max": "1.36", "wording": "Supported Kubernetes versions"},
            "1.29": {"min": "1.33", "max": "1.35", "wording": "Supported Kubernetes versions"},
        })

    def test_a_released_line_is_recorded_once_the_preview_mark_goes(self):
        released = CURRENT.replace("No, preview (RC)", "Yes             ")
        self.assertIn("1.31", self.rows(released + OLD))

    def test_a_current_table_without_its_status_column_needs_a_human(self):
        lines = CURRENT.strip().splitlines()
        no_status = "\n".join("|".join(c for i, c in enumerate(line.split("|")) if i != 2) for line in lines)
        with self.assertRaises(UpstreamChanged):
            cloudnative_pg_rows(no_status + "\n" + OLD, REPO, PATH, COMMIT)


if __name__ == "__main__":
    unittest.main()
