import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import classify_names as cn  # noqa: E402

REAL_NAMES = {
    "DA PIZZA DUDE": "pizza", "INDIAN GARDEN": "indian or south asian", "LAHORI BITES": "indian or south asian",
    "HALAL TRUCK GUYS": "halal or middle eastern", "SHAWARMAN, CORP.": "halal or middle eastern",
    "ME-GA CARNITAS TACO TRUCK": "mexican or latin", "LOS FRIENDS MEXICAN RESTAURANT": "mexican or latin",
    "PANCHO' S ICE CREAM": "dessert and sweets", "MS. TITTLE'S CUPCAKES": "dessert and sweets",
    "PALETERIA REINA DE SABORES": "dessert and sweets", "THE HAPPY LOBSTER": "seafood",
    "EXTREME BURGER": "burgers, bbq and american", "SOUL & SMOKE": "burgers, bbq and american",
    "DUBAI COFFEE": "coffee and drinks", "EL AZTECA #2": "mexican or latin",
    "COMMUNITY CREAMERY": "dessert and sweets", "STALWART MARKET": "unclassified", "TASTY CHICAGO LLC": "unclassified",
    "CHICAGO LUNCHBOX VIN: IFCJF39H3HHB92477 PL:  322709D": "unclassified",
}


class Classify(unittest.TestCase):
    def test_real_names(self):
        for name, want in REAL_NAMES.items():
            self.assertEqual(cn.classify(name)[0], want, name)

    def test_pizza_beats_other_words(self):
        self.assertEqual(cn.classify("Mexican Pizza Truck")[0], "pizza")          # first rule wins, by design

    def test_no_partial_word_matches(self):
        self.assertEqual(cn.classify("Teapot Holdings")[0], "unclassified")       # 'tea' inside a word is not tea
        self.assertEqual(cn.classify("Subtle Things")[0], "unclassified")          # 'sub' inside 'subtle'

    def test_long_keywords_match_word_starts(self):
        self.assertEqual(cn.classify("Tacos Express")[0], "mexican or latin")
        self.assertEqual(cn.classify("Burgerology")[0], "burgers, bbq and american")

    def test_vin_noise_removed_and_spanish_article_is_weak(self):
        self.assertNotIn("vin", cn.normalize("CHIKIS - VIN#1GBHG31U941213597"))
        self.assertIn("weak", cn.classify("El Zorro Food Truck")[1])


class Summary(unittest.TestCase):
    def rows(self):
        names = ["Da Pizza Dude", "Taco Fiesta", "Taco Fiesta", "Stalwart Market", "Anna's Ice Cream"]
        return [{"doing_business_as_name": n, "license_description": "Mobile Food License"} for n in names] + \
               [{"doing_business_as_name": "Cart Co", "license_description": "Tobacco"}]

    def test_counts_add_up_and_distinct_names_dedupe(self):
        rows = [r for r in self.rows() if r["license_description"] == "Mobile Food License"]
        summary, detail = cn.summarize(rows, "doing_business_as_name")
        self.assertEqual(sum(d["rows"] for d in summary.values()), len(rows))
        self.assertEqual(summary["mexican or latin"]["rows"], 2)
        self.assertEqual(len(summary["mexican or latin"]["names"]), 1)
        self.assertEqual(len(detail), 5)

    def test_cli_filters_prints_table_and_writes_csv(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = os.path.join(d, "in.csv"), os.path.join(d, "out.csv")
            with open(src, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["doing_business_as_name", "license_description", "business_activity"])
                w.writeheader()
                for r in self.rows():
                    w.writerow(dict(r, business_activity="x"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cn.main([src, "--where", "license_description=Mobile Food License", "--activity-col",
                              "business_activity", "--out", out])
            text = buf.getvalue()
            with open(out, encoding="utf-8") as f:
                n_out = len(list(csv.DictReader(f)))
        self.assertEqual(rc, 0)
        self.assertIn("5 rows", text)                              # the Tobacco row was filtered out
        self.assertIn("| pizza | 1 |", text)
        self.assertIn("Stalwart Market", text.replace("STALWART MARKET", "Stalwart Market"))
        self.assertEqual(n_out, 5)

    def test_unknown_column_message(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.csv")
            with open(src, "w", encoding="utf-8") as f:
                f.write("a,b\n1,2\n")
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stdout(io.StringIO()):
                    cn.main([src])


if __name__ == "__main__":
    unittest.main()
