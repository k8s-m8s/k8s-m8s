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
        self.assertIn("Most of the site was checked on 2026-12-01. Since 2026-11-28,", page)
        self.assertIn("changed or could not be read", page)
        self.assertNotIn("waiting for review", page)  # it may have merged already; the page can't know

    def test_the_table_has_no_per_row_date(self):
        table = provenance_table(tool()["releases"])
        self.assertNotIn("Verified", table)
        self.assertNotIn("2026-12-01", table)


class SiteCheckedTest(unittest.TestCase):
    def test_the_site_date_is_the_one_most_tools_share(self):
        tools = [tool("a", ("2026-11-23",)), tool("b", ("2026-11-23",)), tool("c", ("2026-11-25",)), tool("d", ("2026-11-26",))]
        self.assertEqual(site_checked(tools), "2026-11-23")

    def test_ties_go_to_the_newer_date(self):
        self.assertEqual(site_checked([tool("a", ("2026-11-23",)), tool("b", ("2026-11-24",))]), "2026-11-24")


class MatrixCalloutTest(unittest.TestCase):
    def test_every_tool_on_the_same_date(self):
        callout = matrix_callout([tool("alpha"), tool("beta")])
        self.assertEqual(callout, '!!! info "Upstream last checked on 2026-12-01"\n'
                                  "    2 tools. Every current entry was compared with its upstream source on this date.\n")

    def test_a_claim_merged_on_a_day_without_bookkeeping_makes_no_tool_look_behind(self):
        # bookkeeping last merged on 11-30; one tool's claim change merged on 12-01
        tools = [tool("alpha", ("2026-11-30",)), tool("beta", ("2026-11-30",)), tool("gamma", ("2026-12-01", "2026-11-30"))]
        callout = matrix_callout(tools)
        self.assertIn("Upstream last checked on 2026-11-30", callout)
        self.assertIn("on this date or later.", callout)
        self.assertNotIn("except", callout)
        self.assertNotIn("warning", tool_page(tools[0], WINDOW, site_checked(tools)))

    def test_a_tool_behind_is_named_with_its_date(self):
        tools = [tool("alpha"), tool("beta", ("2026-11-20", "2026-11-20")), tool("gamma", ("2026-12-01", "2026-11-20"))]
        callout = matrix_callout(tools)
        self.assertIn("except for [Beta](tools/beta.md) (last checked 2026-11-20): the upstream statement changed", callout)
        self.assertNotIn("Alpha", callout)
        self.assertNotIn("Gamma", callout)  # its newest entry is current

    def test_many_tools_behind_are_counted_not_listed(self):
        tools = [tool(f"t{n}") for n in range(6)] + [tool(f"old{n}", ("2026-11-20",)) for n in range(4)]
        callout = matrix_callout(tools)
        self.assertIn("Every current entry of 6 of them was compared", callout)
        self.assertIn("For the other 4,", callout)
        self.assertNotIn("Old0", callout)
        self.assertEqual(callout.count("\n"), 2)  # still a title and one paragraph


if __name__ == "__main__":
    unittest.main()
