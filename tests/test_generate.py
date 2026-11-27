"""Unit tests for generate.py's pages: the "Upstream last checked" callouts and the tool tables."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from generate import last_checked, matrix_callout, provenance_table, site_checked, tool_page  # noqa: E402

WINDOW = ["1.37", "1.36", "1.35"]


def tool(name="alpha", dates=("2026-12-01", "2026-12-01"), delisted_verified=None):
    entries = [{"version": f"1.{n}", "tested": ["1.37.1"], "source": "https://github.com/example/demo/blob/main/README.md",
                "source_commit": "a" * 40, "verified": date} for n, date in enumerate(dates, 1)]
    if delisted_verified:
        entries.append({**entries[0], "version": "0.9", "delisted": "2026-01-01", "verified": delisted_verified})
    return {"name": name, "title": name.title(), "category": "networking", "repo": "https://github.com/example/demo",
            "releases": entries}


class LastCheckedTest(unittest.TestCase):
    def test_a_tool_is_as_recent_as_its_latest_full_check(self):
        # a claim change merged on 12-01 dates only the entry it changed; that read checked the others too
        self.assertEqual(last_checked(tool(dates=("2026-12-01", "2026-11-30"))), "2026-12-01")

    def test_delisted_entries_do_not_count_unless_nothing_is_current(self):
        self.assertEqual(last_checked(tool(dates=("2026-11-30",), delisted_verified="2026-12-05")), "2026-11-30")
        gone = tool(dates=("2026-11-30",))
        gone["releases"][0]["delisted"] = "2026-11-30"
        self.assertEqual(last_checked(gone), "2026-11-30")

    def test_the_site_is_as_recent_as_its_most_recent_tool(self):
        self.assertEqual(site_checked([tool("alpha"), tool("beta", dates=("2026-11-20",))]), "2026-12-01")


class ToolPageTest(unittest.TestCase):
    def test_the_page_opens_with_the_date(self):
        page = tool_page(tool(), WINDOW, "2026-12-01")
        self.assertIn('!!! info "Upstream last checked on 2026-12-01"', page)
        self.assertLess(page.index("Upstream last checked"), page.index("Upstream repository"))

    def test_a_merged_claim_change_leaves_no_warning(self):
        page = tool_page(tool(dates=("2026-12-01", "2026-11-30")), WINDOW, "2026-12-01")
        self.assertIn('!!! info "Upstream last checked on 2026-12-01"', page)
        self.assertNotIn("warning", page)

    def test_a_tool_behind_the_rest_of_the_site_gets_a_warning(self):
        page = tool_page(tool(dates=("2026-11-28", "2026-11-28")), WINDOW, "2026-12-01")
        self.assertIn('!!! warning "Upstream last checked on 2026-11-28"', page)
        self.assertIn("The rest of the site was checked on 2026-12-01", page)
        self.assertIn("changed or could not be read", page)
        self.assertNotIn("waiting for review", page)  # it may have merged already; the page can't know

    def test_the_table_has_no_per_row_date(self):
        table = provenance_table(tool()["releases"])
        self.assertNotIn("Verified", table)
        self.assertNotIn("2026-12-01", table)


class MatrixCalloutTest(unittest.TestCase):
    def test_every_tool_on_the_newest_date(self):
        callout = matrix_callout([tool("alpha"), tool("beta")])
        self.assertTrue(callout.startswith('!!! info "Upstream last checked on 2026-12-01"'))
        self.assertIn("2 tools.", callout)
        self.assertNotIn("except", callout)

    def test_tools_behind_the_newest_date_are_listed_with_their_date(self):
        tools = [tool("alpha"), tool("beta", dates=("2026-11-20", "2026-11-20")), tool("gamma", dates=("2026-12-01", "2026-11-20"))]
        callout = matrix_callout(tools)
        self.assertIn("[Beta](tools/beta.md) (2026-11-20)", callout)
        self.assertIn("changed or could not be read since", callout)
        self.assertNotIn("Alpha", callout)
        self.assertNotIn("Gamma", callout)  # its newest entry is current


if __name__ == "__main__":
    unittest.main()
