import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import event_inventory as ei  # noqa: E402


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = ei.main(list(argv))
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


def write(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def add(d, name, month, cat="food festival", size="5-25k", trucks="yes", sa="n", conf="B"):
    return run("add", "--data", d, "--name", name, "--category", cat, "--month", month, "--size", size,
               "--trucks", trucks, "--south-asian", sa, "--confidence", conf)


class Months(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(ei.parse_months("June"), [6])
        self.assertEqual(ei.parse_months("6"), [6])
        self.assertEqual(ei.parse_months("2027-06"), [6])
        self.assertEqual(ei.parse_months("May-Oct"), [5, 6, 7, 8, 9, 10])
        self.assertEqual(ei.parse_months("Jun; Sep"), [6, 9])
        self.assertEqual(ei.parse_months("Nov-Feb"), [1, 2, 11, 12])          # wraps the year
        self.assertEqual(ei.parse_months("Sept"), [9])
        self.assertEqual(ei.parse_months("weekly"), list(range(1, 13)))
        self.assertEqual(ei.parse_months("late summer"), [])
        self.assertEqual(ei.parse_months(""), [])


class AddAndValidate(unittest.TestCase):
    def test_add_creates_file_and_dedupes(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(add(d, "Taste Fest", "Jun-Jul")[0], 0)
            rc, out = add(d, "Taste Fest", "Jun-Jul")
            self.assertIn("Already listed", out)
            with open(os.path.join(d, "event_inventory.csv"), encoding="utf-8") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_bad_values_rejected_with_reasons(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = add(d, "X", "someday", cat="party", size="huge", trucks="maybe", sa="x", conf="Z")
            self.assertEqual(rc, 2)
            for word in ("category", "typical_month", "size", "trucks", "south-asian", "confidence"):
                self.assertIn(word, out)
            self.assertFalse(os.path.exists(os.path.join(d, "event_inventory.csv")))

    def test_init_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            run("init", "--data", d)
            add(d, "Taste Fest", "Jun")
            rc, out = run("init", "--data", d)
            self.assertIn("not touched", out)


class Coverage(unittest.TestCase):
    def test_thin_months_and_missing_categories(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(3):
                add(d, f"Summer {i}", "Jun-Aug")
            add(d, "Diwali Mela", "Oct-Nov", cat="cultural or south asian", sa="y")
            c = ei.coverage(d)
        self.assertEqual(c["per_month"][7], 3)
        self.assertIn("Jan", c["thin_months"])
        self.assertNotIn("Jul", c["thin_months"])
        self.assertIn("farmers or night market", c["missing_categories"])
        self.assertNotIn("food festival", c["missing_categories"])
        self.assertEqual(c["south_asian_rows"], 1)

    def test_cli_lists_next_searches(self):
        with tempfile.TemporaryDirectory() as d:
            add(d, "Only One", "Jun")
            rc, out = run("coverage", "--data", d)
        self.assertIn("<-- thin", out)
        self.assertIn("Next searches", out)
        self.assertIn("Diwali", out)
        self.assertIn("farmers or night market", out)          # categories are not crowded out by the months
        self.assertEqual(out.count("one search per month"), 1)

    def test_empty_inventory(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = run("coverage", "--data", d)
        self.assertEqual(rc, 0)
        self.assertIn("Thin months", out)

    def test_ticketed_events_are_counted_but_not_hide_thin_recurring(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "events_ticketmaster.csv"), ["date", "segment"],
                  [{"date": "2027-01-10", "segment": "Music"}] * 5)
            write(os.path.join(d, "sports_home_games.csv"), ["date", "team"], [{"date": "2027-01-12", "team": "X"}])
            c = ei.coverage(d)
        self.assertEqual(c["ticketed"][1], 6)
        self.assertIn("Jan", c["thin_months"])


class MajorEventsAndCaps(unittest.TestCase):
    def test_one_venue_cannot_dominate_the_ticketed_counts(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [{"date": "2027-01-10", "segment": "Miscellaneous", "venue": "The Fields Studios"}] * 500
            rows += [{"date": "2027-01-11", "segment": "Music", "venue": "United Center"}] * 3
            write(os.path.join(d, "events_ticketmaster.csv"), ["date", "segment", "venue"], rows)
            c = ei.coverage(d)
        self.assertEqual(c["ticketed"][1], ei.VENUE_MONTH_CAP + 3)

    def test_coverage_asks_for_major_events_and_names_places_without_one(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "acs_places_core.csv"), ["name"], [{"name": "Chicago"}, {"name": "Naperville"},
                                                                    {"name": "Skokie"}])
            run("add", "--data", d, "--name", "Taste of Chicago", "--category", "food festival", "--month", "Jul",
                "--venue", "Grant Park, Chicago", "--size", "25k+", "--trucks", "unknown", "--south-asian", "n",
                "--confidence", "C")
            c = ei.coverage(d)
            rc, out = run("coverage", "--data", d)
        self.assertEqual(c["places_without_event"], ["Naperville", "Skokie"])
        self.assertEqual(c["large_events"], 1)
        self.assertIn("major events: search the largest annual events", out)
        self.assertIn("no event listed for: Naperville, Skokie", out)

    def test_top_ten_lists_each_event_series_once(self):
        with tempfile.TemporaryDirectory() as d:
            for m in ("May", "Jul", "Oct"):
                add(d, f"Chi Food Truck Fest ({m} run)", m, size="25k+")
            add(d, "Other Fest", "Aug", size="5-25k")
            rc, out = run("report", "--data", d)
        rows = [l for l in out.splitlines() if re.match(r"\| \d+ \|", l)]
        self.assertEqual(len(rows), 2)


class Report(unittest.TestCase):
    def test_tables_and_ranking(self):
        with tempfile.TemporaryDirectory() as d:
            add(d, "Big Open Fest", "Jul", size="25k+", trucks="yes", conf="A")
            add(d, "No Trucks Fair", "Aug", size="25k+", trucks="no")
            add(d, "Garba Night", "Oct", cat="cultural or south asian", size="1-5k", trucks="unknown", sa="y")
            rc, out = run("report", "--data", d)
        self.assertIn("| Jul | Big Open Fest (A)", out)
        self.assertIn("| Jan | Not found", out)
        rows = [l for l in out.splitlines() if re.match(r"\| \d+ \|", l)]
        self.assertTrue(rows[0].startswith("| 1 | Big Open Fest"))
        self.assertIn("No Trucks Fair", rows[-1])            # a truck ban sinks it to the bottom
        self.assertIn("South Asian audience", out)

    def test_pick_score_ordering(self):
        a = {"size_tier": "25k+", "food_trucks_allowed": "yes", "south_asian": "n", "confidence": "A", "typical_month": "Jul"}
        b = dict(a, food_trucks_allowed="no")
        c = dict(a, size_tier="1-5k")
        self.assertGreater(ei.pick_score(a), ei.pick_score(c))
        self.assertGreater(ei.pick_score(c), ei.pick_score(b))


import re  # noqa: E402


class Demand(unittest.TestCase):
    def pack(self, d, with_events=True):
        write(os.path.join(d, "acs_metro.csv"), ["population", "asian_indian_pct_of_pop"],
              [{"population": "9000000", "asian_indian_pct_of_pop": "1.5"}])
        write(os.path.join(d, "noaa_monthly_normals.csv"), ["month", "days_high_ge_50f", "days_precip_ge_0.10in"],
              [{"month": m, "days_high_ge_50f": [1, 3, 12, 22, 29, 30, 31, 31, 29, 21, 9, 3][m - 1],
                "days_precip_ge_0.10in": 6} for m in range(1, 13)])
        write(os.path.join(d, "airport_seasonality_index.csv"), ["month", "index"],
              [{"month": m, "index": 100 + (15 if m in (6, 7, 8) else -10 if m in (1, 2) else 0)} for m in range(1, 13)])
        write(os.path.join(d, "campus_enrollment.csv"), ["name", "est_graduates_per_year", "degree_granting"],
              [{"name": "U", "est_graduates_per_year": "8000", "degree_granting": "1"},
               {"name": "Beauty", "est_graduates_per_year": "99", "degree_granting": "0"}])
        write(os.path.join(d, "k12_seniors.csv"), ["grade12_enrollment"], [{"grade12_enrollment": "1000"}] * 3)
        if with_events:
            for i, m in enumerate(("Jun", "Jul", "Aug", "Sep", "Jun-Aug", "Jul-Sep", "May-Jun", "Aug-Oct", "Sep-Oct",
                                   "Jun", "Jul", "Aug")):
                add(d, f"Fest {i} {m}", m)

    def test_full_pack_output(self):
        with tempfile.TemporaryDirectory() as d:
            self.pack(d)
            rc, out = run("demand", "--data", d)
        self.assertEqual(rc, 0)
        self.assertIn("Demand index (0-100)", out)
        self.assertIn("method:", out)
        self.assertIn("weddings (est. shape)", out)
        self.assertIn("est. weddings per year in the metro: about 54,000", out)       # 9,000,000 x 6 / 1000
        self.assertIn("South Asian weddings if the same rate applied: about 810", out)
        self.assertIn("about 8,000", out)                                             # only the degree-granting campus
        self.assertIn("about 3,000", out)
        feb = [l for l in out.splitlines() if l.startswith("| Feb")][0]
        jul = [l for l in out.splitlines() if l.startswith("| Jul")][0]
        self.assertGreater(float(jul.rstrip(" |").split("|")[-1]), float(feb.rstrip(" |").split("|")[-1]))

    def test_events_column_needs_a_real_calendar(self):
        with tempfile.TemporaryDirectory() as d:
            self.pack(d, with_events=False)
            write(os.path.join(d, "sports_home_games.csv"), ["date", "team"],
                  [{"date": "2027-06-10", "team": "X"}] * 20)             # baseball only: not a calendar
            rc, out = run("demand", "--data", d)
        header = [l for l in out.splitlines() if l.startswith("| Month")][0]
        self.assertNotIn("events (", header)
        self.assertIn("column is left out", out)

    def test_events_column_appears_with_ticketmaster_or_enough_rows(self):
        with tempfile.TemporaryDirectory() as d:
            self.pack(d, with_events=False)
            write(os.path.join(d, "events_ticketmaster.csv"), ["date", "segment"],
                  [{"date": "2027-06-10", "segment": "Music"}])
            rc, out = run("demand", "--data", d)
        self.assertIn("events (", [l for l in out.splitlines() if l.startswith("| Month")][0])

    def test_weights_renormalize_when_inputs_are_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.pack(d, with_events=False)
            os.remove(os.path.join(d, "airport_seasonality_index.csv"))
            rc, out = run("demand", "--data", d)
        header = [l for l in out.splitlines() if l.startswith("| Month")][0]
        self.assertNotIn("events", header)
        self.assertNotIn("visitors", header)
        pct = [int(x) for x in re.findall(r"\((\d+)%\)", header)]
        self.assertIn(sum(pct), (99, 100, 101))

    def test_runs_with_nothing_pulled(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = run("demand", "--data", d)
        self.assertEqual(rc, 0)
        self.assertIn("est.", out)


class CityArgument(unittest.TestCase):
    def test_city_maps_to_reports_folder(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                rc, out = run("init", "Chicago, IL")
                self.assertTrue(os.path.exists(os.path.join("reports", "chicago-il", "data", "event_inventory.csv")))
            finally:
                os.chdir(old)

    def test_missing_city_and_data_is_an_error(self):
        rc, out = run("coverage")
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()


class Neighborhoods(unittest.TestCase):
    def rows(self):
        return [{"name": "Loop", "breweries": "5", "bars_pubs": "90", "restaurants": "300", "colleges_universities": "6"},
                {"name": "Wicker Park", "breweries": "3", "bars_pubs": "60", "restaurants": "150", "colleges_universities": "0"},
                {"name": "Edgebrook", "breweries": "0", "bars_pubs": "2", "restaurants": "20", "colleges_universities": "0"},
                {"name": "Hyde Park", "breweries": "1", "bars_pubs": "10", "restaurants": "60", "colleges_universities": "4"},
                {"name": "Bridgeport", "breweries": "1", "bars_pubs": "12", "restaurants": "40", "colleges_universities": "0"}]

    def test_quintiles(self):
        self.assertEqual(ei.quintile_scores([1, 2, 3, 4, 5]), [1, 2, 3, 4, 5])
        self.assertEqual(ei.quintile_scores([7, 7, 7, 7, 7]), [3, 3, 3, 3, 3])              # all tied: middle
        self.assertEqual(ei.quintile_scores([10, 0]), [5, 3])

    def test_ranking_orders_busy_districts_first(self):
        scored = ei.score_neighborhoods(self.rows())
        self.assertEqual(scored[0]["name"], "Loop")
        self.assertEqual(scored[-1]["name"], "Edgebrook")
        self.assertEqual(scored[0]["restaurants_bars"], 390)

    def test_cli_prints_table_method_and_limits(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "neighborhood_counts_osm.csv"),
                  ["name", "breweries", "bars_pubs", "restaurants", "colleges_universities"], self.rows())
            rc, out = run("neighborhoods", "--data", d)
        self.assertEqual(rc, 0)
        self.assertIn("| 1 | Loop |", out)
        self.assertIn("NO population or income signal", out)
        self.assertIn("low confidence", out)

    def test_missing_file_explains(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = run("neighborhoods", "--data", d)
        self.assertEqual(rc, 1)
        self.assertIn("--neighborhoods-only", out)
