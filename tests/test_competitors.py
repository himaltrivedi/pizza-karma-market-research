import contextlib
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import competitors as cp  # noqa: E402


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = cp.main(list(argv))
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


def add(d, name="Spicy Desi", cuisine="indian or south asian", kind="truck", **kw):
    args = ["add", "--data", d, "--name", name, "--cuisine", cuisine, "--kind", kind]
    for k, v in kw.items():
        args += [f"--{k.replace('_', '-')}", str(v)]
    return run(*args)


class Competitors(unittest.TestCase):
    def test_validation(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = add(d, cuisine="tacos", kind="van", oven="maybe", confidence="Z", price_low="12")
            self.assertEqual(rc, 2)
            for word in ("cuisine", "kind", "oven", "confidence", "both price_low and price_high"):
                self.assertIn(word, out)
            rc, out = add(d, price_low="20", price_high="10", price_item="main")
            self.assertIn("higher than price_high", out)
            rc, out = add(d, price_low="10", price_high="20")
            self.assertIn("price_item is required", out)
            rc, out = add(d, rating="4.5")
            self.assertIn("--platform", out)
            rc, out = add(d, rating="45", platform="Yelp")
            self.assertIn("between 0 and 10", out)

    def test_add_replaces_same_vendor_and_item(self):
        with tempfile.TemporaryDirectory() as d:
            add(d, price_item="main", price_low="10", price_high="12")
            add(d, price_item="main", price_low="11", price_high="14")
            add(d, price_item="drink", price_low="3", price_high="4")
            rows = cp.read_rows(os.path.join(d, "competitors.csv"))
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["price_high"] for r in rows if r["price_item"] == "main"], ["14"])

    def test_priced_row_replaces_the_placeholder_instead_of_duplicating(self):
        with tempfile.TemporaryDirectory() as d:
            add(d, name="Billy Bricks", cuisine="pizza", kind="truck", area="Lombard", oven="yes")
            add(d, name="Billy Bricks", cuisine="pizza", kind="truck", price_item="food per person (starting)",
                price_low="34", price_high="34", rating="4.7", platform="Zola", reviews="92")
            rows = cp.read_rows(os.path.join(d, "competitors.csv"))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["area"], rows[0]["oven"], rows[0]["price_low"]), ("Lombard", "yes", "34"))

    def test_unpriced_row_folds_into_the_existing_vendor(self):
        with tempfile.TemporaryDirectory() as d:
            add(d, name="Billy Bricks", cuisine="pizza", kind="truck", price_item="per person", price_low="34",
                price_high="34")
            add(d, name="Billy Bricks", cuisine="pizza", kind="truck", rating="4.7", platform="Zola", reviews="92",
                booking="Get a quote", confidence="B")
            rows = cp.read_rows(os.path.join(d, "competitors.csv"))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["rating"], rows[0]["review_count"], rows[0]["booking"], rows[0]["confidence"]),
                         ("4.7", "92", "Get a quote", "B"))

    def test_price_items_with_notes_group_together_in_the_report(self):
        with tempfile.TemporaryDirectory() as d:
            add(d, name="A", cuisine="pizza", kind="truck", price_item="per person (2026)", price_low="30", price_high="30")
            add(d, name="B", cuisine="pizza", kind="caterer", price_item="per person (2008 article)", price_low="11",
                price_high="16")
            rc, out = run("report", "--data", d)
        self.assertEqual(out.count("| Pizza | per person |"), 1)
        self.assertIn("| Pizza | per person | $11 | $30 | 2 |", out)

    def test_coverage_lists_gaps_and_the_oven_caveat(self):
        with tempfile.TemporaryDirectory() as d:
            add(d, name="Da Pizza Dude", cuisine="pizza", kind="truck")
            add(d, name="Desi Boys Pizza", cuisine="indian-fusion pizza", kind="restaurant", price_item="pie",
                price_low="10", price_high="30", rating="7.2", platform="The Infatuation (0-10)", reviews="1")
            rc, out = run("coverage", "--data", d)
        self.assertIn("pizza 2 of up to 8", out)
        self.assertIn("Indian/South Asian 1 of up to 5", out)
        self.assertIn("Da Pizza Dude: still need a menu price, a rating, a review count", out)
        self.assertNotIn("Desi Boys Pizza: still need a menu price", out)
        self.assertIn("say how many you checked", out)

    def test_report_price_ranges_and_menus_sampled(self):
        with tempfile.TemporaryDirectory() as d:
            add(d, name="A Pizza", cuisine="pizza", kind="restaurant", price_item="pie", price_low="12", price_high="18")
            add(d, name="B Pizza", cuisine="pizza", kind="truck", price_item="pie", price_low="9", price_high="22")
            add(d, name="C Desi", price_item="main", price_low="11", price_high="15")
            rc, out = run("report", "--data", d)
        self.assertIn("| Pizza | pie | $9 | $22 | 2 |", out)
        self.assertIn("| Indian / South Asian | main | $11 | $15 | 1 |", out)
        self.assertIn("pie: $12-$18", out)

    def test_empty_report_and_city_argument(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("no vendors recorded", run("report", "--data", d)[1])
            old = os.getcwd()
            os.chdir(d)
            try:
                rc, out = add_city()
                self.assertEqual(rc, 0)
                self.assertTrue(os.path.exists(os.path.join("reports", "chicago-il", "data", "competitors.csv")))
            finally:
                os.chdir(old)


def add_city():
    return run("add", "Chicago, IL", "--name", "X", "--cuisine", "pizza", "--kind", "truck")


if __name__ == "__main__":
    unittest.main()
