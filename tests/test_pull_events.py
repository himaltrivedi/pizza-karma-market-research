"""Offline tests for scripts/pull_events.py (every endpoint mocked)."""
import csv
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import pull_events as pe  # noqa: E402

pd = pe.pd
CHI = (41.8369, -87.6847)


def tm_event(i, date="2027-06-01", seg="Music", venue="United Center"):
    return {"id": f"e{i}", "name": f"Show {i}", "dates": {"start": {"localDate": date, "localTime": "19:30:00"}},
            "classifications": [{"segment": {"name": seg}, "genre": {"name": "Rock"}}],
            "_embedded": {"venues": [{"name": venue, "city": {"name": "Chicago"},
                                      "location": {"latitude": "41.88", "longitude": "-87.67"}}]}}


def tm_page(total, events, pages=1):
    return json.dumps({"_embedded": {"events": events}, "page": {"totalElements": total, "totalPages": pages}}).encode()


class Dates(unittest.TestCase):
    def test_month_ranges_cover_a_year_without_gaps(self):
        rng = pe.month_ranges(dt.date(2026, 11, 3), 12)
        self.assertEqual(rng[0], (dt.date(2026, 11, 3), dt.date(2026, 11, 30)))
        self.assertEqual(rng[1][0], dt.date(2026, 12, 1))
        self.assertEqual(rng[2][0], dt.date(2027, 1, 1))
        self.assertEqual(rng[-1][1], dt.date(2027, 10, 31))
        for (_, e1), (s2, _) in zip(rng, rng[1:]):
            self.assertEqual(e1 + dt.timedelta(days=1), s2)


class Ticketmaster(unittest.TestCase):
    def test_parse_and_summaries(self):
        rows = [pe.tm_parse(tm_event(1)), pe.tm_parse(tm_event(2, "2027-06-15", "Sports", "Wrigley Field")),
                pe.tm_parse(tm_event(3, "2027-07-01"))]
        self.assertEqual(rows[0]["venue"], "United Center")
        self.assertEqual(rows[1]["segment"], "Sports")
        m = {(r["month"], r["segment"]): r["events"] for r in pe.events_by_month(rows)}
        self.assertEqual(m[("2027-06", "Music")], 1)
        self.assertEqual(m[("2027-06", "Sports")], 1)
        self.assertEqual(pe.venues_top(rows)[0], {"venue": "United Center", "city": "Chicago", "events": 2})

    def test_venue_cap_keeps_one_venue_from_dominating_a_month(self):
        rows = [{"date": "2027-01-10", "segment": "Miscellaneous", "venue": "The Fields Studios"}] * 50
        rows += [{"date": "2027-01-11", "segment": "Music", "venue": "United Center"}] * 3
        uncapped = {(r["month"], r["segment"]): r["events"] for r in pe.events_by_month(rows)}
        capped = {(r["month"], r["segment"]): r["events"] for r in pe.events_by_month(rows, pe.VENUE_MONTH_CAP)}
        self.assertEqual(uncapped[("2027-01", "Miscellaneous")], 50)
        self.assertEqual(capped[("2027-01", "Miscellaneous")], pe.VENUE_MONTH_CAP)
        self.assertEqual(capped[("2027-01", "Music")], 3)

    def test_range_over_cap_is_split_and_deduped(self):
        calls = []

        def fake(url, *a, **k):
            calls.append(url)
            q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
            start, end = q["startDateTime"][:10], q["endDateTime"][:10]
            span = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
            if span >= 10:
                return tm_page(1500, [tm_event(1)])            # too many: must be split
            return tm_page(2, [tm_event(1), tm_event(2)])
        out = {}
        with mock.patch.object(pd, "http_get", fake), mock.patch("time.sleep"):
            pe.tm_fetch_range("k", *CHI, 35, dt.date(2027, 6, 1), dt.date(2027, 6, 30), "Music", out, pause=0)
        self.assertEqual(set(out), {"e1", "e2"})
        self.assertGreater(len(calls), 3)
        self.assertTrue(all("segmentName=Music" in u for u in calls))

    def test_paging_stops_at_deep_limit(self):
        urls = []

        def fake(url, *a, **k):
            urls.append(url)
            return tm_page(900, [tm_event(len(urls) * 10 + j) for j in range(2)], pages=5)
        with mock.patch.object(pd, "http_get", fake), mock.patch("time.sleep"):
            pe.tm_fetch_range("k", *CHI, 35, dt.date(2027, 6, 1), dt.date(2027, 6, 2), "Music", {}, pause=0)
        self.assertEqual(len(urls), 5)                          # 5 pages of 200 = the 1000 cap

    def test_no_events_makes_one_call(self):
        urls = []
        with mock.patch.object(pd, "http_get", lambda u, *a, **k: urls.append(u) or tm_page(0, [])):
            pe.tm_fetch_range("k", *CHI, 35, dt.date(2027, 6, 1), dt.date(2027, 6, 30), "Film", {}, pause=0)
        self.assertEqual(len(urls), 1)

    def test_key_lookup(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            os.environ.pop("TICKETMASTER_API_KEY", None)
            self.assertIsNone(pe.get_ticketmaster_key())
            with open(os.path.join(home, ".ticketmaster_key"), "w") as f:
                f.write("abc\n")
            self.assertEqual(pe.get_ticketmaster_key(), "abc")
            self.assertEqual(pe.get_ticketmaster_key("cli"), "cli")


MLB_TEAMS = {"teams": [
    {"id": 112, "name": "Chicago Cubs", "venue": {"id": 17, "name": "Wrigley Field",
     "location": {"defaultCoordinates": {"latitude": 41.9484, "longitude": -87.6553}}}},
    {"id": 145, "name": "Chicago White Sox", "venue": {"id": 4, "name": "Rate Field",
     "location": {"defaultCoordinates": {"latitude": 41.83, "longitude": -87.634}}}},
    {"id": 119, "name": "Los Angeles Dodgers", "venue": {"id": 22, "name": "Dodger Stadium",
     "location": {"defaultCoordinates": {"latitude": 34.07, "longitude": -118.24}}}}]}


def mlb_schedule(team_name, venue_id, other_venue_id=99):
    return json.dumps({"dates": [
        {"date": "2027-04-02", "games": [{"gameType": "R", "venue": {"id": venue_id},
                                          "teams": {"away": {"team": {"name": "Rival"}}}}]},
        {"date": "2027-04-05", "games": [{"gameType": "R", "venue": {"id": other_venue_id},
                                          "teams": {"away": {"team": {"name": team_name}}}}]}]}).encode()


class Mlb(unittest.TestCase):
    def test_only_metro_teams_and_home_games(self):
        def fake(url, *a, **k):
            if "/teams" in url:
                return json.dumps(MLB_TEAMS).encode()
            if "teamId=112" in url:
                return mlb_schedule("Chicago Cubs", 17)
            if "teamId=145" in url:
                return mlb_schedule("Chicago White Sox", 4)
            raise AssertionError("Dodgers are far away and must not be requested: " + url)
        with mock.patch.object(pd, "http_get", fake):
            rows = pe.mlb_home_games(*CHI, 35, dt.date(2027, 3, 1), dt.date(2027, 10, 1))
        self.assertEqual({r["team"] for r in rows}, {"Chicago Cubs", "Chicago White Sox"})
        self.assertEqual(len(rows), 2)                      # the away game is excluded
        self.assertTrue(all(r["opponent"] == "Rival" for r in rows))
        by = pe.sports_by_month(rows)
        self.assertEqual({(b["month"], b["home_games"]) for b in by}, {("2027-04", 1)})


AIRPORTS = (
    '"id","ident","type","name","latitude_deg","longitude_deg","iso_country","scheduled_service","iata_code"\n'
    '1,KORD,large_airport,Chicago O\'Hare Intl,41.9786,-87.9048,US,yes,ORD\n'
    '2,KMDW,medium_airport,Chicago Midway Intl,41.7868,-87.7522,US,yes,MDW\n'
    '3,KPWK,small_airport,Chicago Executive,42.114,-87.9015,US,no,\n'
    '4,KLAX,large_airport,Los Angeles Intl,33.9425,-118.408,US,yes,LAX\n'
    '5,KJOT,medium_airport,Joliet Regional,41.518,-88.175,US,no,JOT\n')


class Airports(unittest.TestCase):
    def test_nearest_scheduled_airports_large_first(self):
        got = pe.airports_near(AIRPORTS, *CHI)
        self.assertEqual([a["iata"] for a in got], ["ORD", "MDW"])

    def rows(self, years, skip=None):
        out = []
        for y in years:
            for m in range(1, 13):
                if skip == (y, m):
                    continue
                out.append({"airport": "ORD", "month": f"{y}-{m:02d}", "passengers": 1000 + (200 if m in (6, 7) else 0)})
        return out

    def test_index_is_100_on_average_and_peaks_in_summer(self):
        idx = pe.seasonality_index(self.rows(["2023", "2024", "2025"]))
        self.assertEqual(len(idx), 12)
        self.assertAlmostEqual(sum(r["index"] for r in idx) / 12, 100, delta=0.2)
        self.assertGreater(idx[5]["index"], idx[0]["index"])
        self.assertEqual(idx[0]["years_used"], "2023,2024,2025")

    def test_incomplete_year_excluded(self):
        idx = pe.seasonality_index(self.rows(["2023", "2024", "2025"], skip=("2025", 3)))
        self.assertEqual(idx[0]["years_used"], "2023,2024")

    def test_no_complete_year_gives_nothing(self):
        self.assertEqual(pe.seasonality_index(self.rows(["2025"], skip=("2025", 3))), [])

    def test_bts_rows_parse(self):
        body = json.dumps([{"reporting_month": "2024-01-01T00:00:00.000", "pax": "2402620"}]).encode()
        with mock.patch.object(pd, "http_get", lambda *a, **k: body):
            self.assertEqual(pe.bts_month_rows("ORD", 2023), [{"airport": "ORD", "month": "2024-01", "passengers": 2402620}])


def cbp_json(rows):
    return json.dumps([["NAME", "ESTAB", "EMP", "NAICS2017", "NAICS2017", "state", "county"]] + rows).encode()


class Cbp(unittest.TestCase):
    def test_pull_filters_to_metro_counties_and_totals(self):
        def fake(url, *a, **k):
            code = url.split("NAICS2017=")[-1].split("&")[0]
            return cbp_json([["Cook County, Illinois", "64", "204", code, code, "17", "031"],
                             ["DuPage County, Illinois", "15", "34", code, code, "17", "043"],
                             ["Winnebago County, Illinois", "4", "9", code, code, "17", "201"]])
        with mock.patch.object(pd, "http_get", fake):
            rows, year = pe.cbp_pull("k", {"17031", "17043"}, dt.date(2026, 11, 3))
        self.assertEqual(year, 2024)
        self.assertEqual({r["county_fips"] for r in rows}, {"17031", "17043"})
        tot = {t["naics"]: t for t in pe.cbp_metro_totals(rows)}
        self.assertEqual(tot["722330"]["establishments"], 79)
        self.assertEqual(tot["722330"]["employees"], 238)
        self.assertEqual(tot["722330"]["counties"], 2)

    def test_falls_back_to_earlier_year(self):
        def fake(url, *a, **k):
            if "/2024/cbp" in url:
                raise pd.HttpError(404, "no such vintage")
            return cbp_json([["Cook County, Illinois", "1", "2", "x", "x", "17", "031"]])
        with mock.patch.object(pd, "http_get", fake):
            _, year = pe.cbp_pull("k", {"17031"}, dt.date(2026, 11, 3))
        self.assertEqual(year, 2023)

    def test_bad_key_is_a_key_error(self):
        with mock.patch.object(pd, "http_get", return_value=b"<html>Invalid Key</html>"):
            with self.assertRaises(pd.CensusKeyError):
                pe.cbp_query(2023, "17", "722330", "bad")
        with self.assertRaises(pd.CensusKeyError):
            pe.cbp_query(2023, "17", "722330", "")


class Urban(unittest.TestCase):
    def test_ipeds_keeps_metro_colleges_only(self):
        rows = [{"unitid": 1, "enrollment_fall": 8560}, {"unitid": 2, "enrollment_fall": 400},
                {"unitid": 99, "enrollment_fall": 5000}, {"unitid": 1, "enrollment_fall": -1}]
        colleges = {"1": {"inst_name": "Big U", "city": "Chicago", "county_name": "Cook County", "degree_granting": "1"},
                    "2": {"inst_name": "Small C", "city": "Evanston", "county_name": "Cook County", "degree_granting": "1"}}
        with mock.patch.object(pd, "urban_fetch_all", return_value=rows):
            out, y = pe.ipeds_enrollment(["17"], colleges, dt.date(2026, 11, 3))
        self.assertEqual([r["name"] for r in out], ["Big U", "Small C"])       # sorted, unknown and negative dropped
        self.assertEqual(out[0]["est_graduates_per_year"], 2140)
        self.assertEqual(y, 2024)

    def test_ccd_keeps_totals_for_metro_districts(self):
        rows = [{"leaid": "1700001", "race": 99, "sex": 99, "enrollment": 31},
                {"leaid": "1700001", "race": 1, "sex": 99, "enrollment": 10},
                {"leaid": "1799999", "race": 99, "sex": 99, "enrollment": 500}]
        dist = {"1700001": {"lea_name": "D1", "city_location": "Chicago", "county_name": "Cook County", "agency_type": "1"}}
        with mock.patch.object(pd, "urban_fetch_all", return_value=rows):
            out, _ = pe.ccd_seniors(["17"], dist, dt.date(2026, 11, 3))
        self.assertEqual([(r["district"], r["grade12_enrollment"]) for r in out], [("D1", 31)])

    def test_timeout_is_not_retried_across_years(self):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise pd.NetworkError("timed out")
        with mock.patch.object(pd, "urban_fetch_all", boom):
            with self.assertRaises(pd.NetworkError):
                pe.urban_year("x/{year}/", {}, dt.date(2026, 11, 3))
        self.assertEqual(len(calls), 1)


def catalog(items):
    return json.dumps({"results": items}).encode()


def cat_item(id_, name, domain, cols=("date", "venue")):
    return {"resource": {"id": id_, "name": name, "description": "Upcoming events", "columns_name": list(cols),
                         "data_updated_at": "2026-10-01T00:00:00Z"}, "metadata": {"domain": domain}}


class Socrata(unittest.TestCase):
    def test_preset_domain_dedupes_across_terms(self):
        with mock.patch.object(pd, "http_get", lambda *a, **k: catalog([cat_item("xgse-8eg7", "Special Events",
                                                                               "data.cityofchicago.org")])):
            rows = pe.socrata_candidates("chicago-il", "Chicago", None)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["has_date_column"])
        self.assertEqual(rows[0]["updated"], "2026-10-01")
        self.assertFalse(rows[0]["stale"])

    def test_unknown_city_keeps_only_matching_domains(self):
        items = [cat_item("a", "Events", "data.exampleville.gov"), cat_item("b", "Events", "data.othertown.gov")]
        with mock.patch.object(pd, "http_get", lambda *a, **k: catalog(items)):
            rows = pe.socrata_candidates("exampleville-oh", "Exampleville", None)
        self.assertEqual([r["id"] for r in rows], ["a"])

    def test_junk_dropped_and_stale_flagged_and_sorted(self):
        items = [cat_item("a", "FOIA Request Log - Special Events", "data.cityofchicago.org"),
                 cat_item("b", "Relocated Vehicles", "data.cityofchicago.org"),
                 cat_item("c", "Farmers Markets - 2012", "data.cityofchicago.org"),
                 cat_item("d", "Special Events", "data.cityofchicago.org")]
        items[2]["resource"]["data_updated_at"] = "2012-05-24T00:00:00Z"
        with mock.patch.object(pd, "http_get", lambda *a, **k: catalog(items)):
            rows = pe.socrata_candidates("chicago-il", "Chicago", None)
        self.assertEqual([r["id"] for r in rows], ["d", "c"])
        self.assertEqual([r["stale"] for r in rows], [False, True])

    def test_save_dataset_pages_and_flattens(self):
        pages = [[{"a": i, "loc": {"x": 1}, ":sys": 1} for i in range(5)], [{"a": 9}]]
        calls = []

        def fake(url, *a, **k):
            calls.append(url)
            return json.dumps(pages[len(calls) - 1]).encode()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(pd, "http_get", fake):
            n = pe.save_dataset("d.example", "abcd-1234", os.path.join(d, "x.csv"), page=5)
            with open(os.path.join(d, "x.csv"), encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(n, 6)
        self.assertNotIn(":sys", rows[0])
        self.assertEqual(json.loads(rows[0]["loc"]), {"x": 1})


def write(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


class RunEndToEnd(unittest.TestCase):
    def setup_pull(self, d):
        data = os.path.join(d, "data")
        write(os.path.join(data, "acs_places_core.csv"), ["name", "state", "lat", "lon"],
              [{"name": "Chicago", "state": "IL", "lat": CHI[0], "lon": CHI[1]}])
        write(os.path.join(data, "metro_counties.csv"), ["county_fips", "county_name"],
              [{"county_fips": "17031", "county_name": "Cook County"}])
        write(os.path.join(data, "colleges_metro.csv"), ["unitid", "inst_name", "city", "county_name", "degree_granting"],
              [{"unitid": "1", "inst_name": "Big U", "city": "Chicago", "county_name": "Cook County", "degree_granting": "1"}])
        write(os.path.join(data, "schools_districts_metro.csv"),
              ["leaid", "lea_name", "city_location", "county_name", "agency_type"],
              [{"leaid": "1700001", "lea_name": "D1", "city_location": "Chicago", "county_name": "Cook County",
                "agency_type": "1"}])
        with open(os.path.join(data, "MANIFEST.md"), "w") as f:
            f.write("# existing manifest\n")
        return data

    def fake(self, url, *a, **k):
        if "ticketmaster" in url:
            return tm_page(1, [tm_event(1)])
        if "statsapi.mlb.com" in url and "/teams" in url:
            return json.dumps(MLB_TEAMS).encode()
        if "statsapi.mlb.com" in url:
            return mlb_schedule("Chicago Cubs", 17)
        if "ourairports" in url:
            return AIRPORTS.encode()
        if "data.bts.gov" in url:
            return json.dumps([{"reporting_month": f"{y}-{m:02d}-01T00:00:00.000", "pax": "1000"}
                               for y in (2023, 2024, 2025) for m in range(1, 13)]).encode()
        if "/cbp" in url:
            return cbp_json([["Cook County, Illinois", "64", "204", "x", "x", "17", "031"]])
        if "fall-enrollment" in url:
            return json.dumps({"results": [{"unitid": 1, "enrollment_fall": 8000}], "next": None}).encode()
        if "ccd/enrollment" in url:
            return json.dumps({"results": [{"leaid": "1700001", "race": 99, "sex": 99, "enrollment": 40}],
                               "next": None}).encode()
        if "api.us.socrata.com" in url:
            return catalog([cat_item("xgse-8eg7", "Special Events", "data.cityofchicago.org")])
        raise AssertionError("unexpected url " + url)

    def args(self, d, **kw):
        base = dict(city="Chicago, IL", outdir=d, steps=None, radius=35.0, months=1, tm_key=None, census_key="ck",
                    socrata_domain=None, dataset=None)
        base.update(kw)
        return pe.argparse.Namespace(**base)

    def run_it(self, d, **kw):
        with mock.patch.object(pd, "http_get", self.fake), mock.patch("time.sleep"), \
                mock.patch.dict(os.environ, {"HOME": d, "USERPROFILE": d}):
            os.environ.pop("TICKETMASTER_API_KEY", None)
            return pe.run(self.args(d, **kw))

    def test_all_steps_with_a_ticketmaster_key(self):
        with tempfile.TemporaryDirectory() as d:
            data = self.setup_pull(d)
            rc = self.run_it(d, tm_key="tk")
            files = set(os.listdir(data))
            manifest = open(os.path.join(data, "MANIFEST.md"), encoding="utf-8").read()
        self.assertEqual(rc, 0)
        for f in ("events_ticketmaster.csv", "events_by_month.csv", "venues_top.csv", "sports_home_games.csv",
                  "sports_by_month.csv", "airport_monthly_passengers.csv", "airport_seasonality_index.csv",
                  "business_patterns_county.csv", "business_patterns_metro.csv", "campus_enrollment.csv",
                  "k12_seniors.csv", "city_open_data_candidates.csv", "events_pull_log.json"):
            self.assertIn(f, files)
        self.assertTrue(manifest.startswith("# existing manifest"))
        self.assertIn("Events and demand data", manifest)

    def test_without_ticketmaster_key_everything_else_still_runs(self):
        with tempfile.TemporaryDirectory() as d:
            data = self.setup_pull(d)
            rc = self.run_it(d)
            files = set(os.listdir(data))
            manifest = open(os.path.join(data, "MANIFEST.md"), encoding="utf-8").read()
        self.assertEqual(rc, 0)
        self.assertNotIn("events_ticketmaster.csv", files)
        self.assertIn("campus_enrollment.csv", files)
        self.assertIn("No Ticketmaster key", manifest)

    def test_one_failing_step_does_not_stop_the_others(self):
        def fake(url, *a, **k):
            if "statsapi.mlb.com" in url:
                raise pd.NetworkError("down")
            return self.fake(url, *a, **k)
        with tempfile.TemporaryDirectory() as d:
            data = self.setup_pull(d)
            with mock.patch.object(pd, "http_get", fake), mock.patch("time.sleep"), \
                    mock.patch.dict(os.environ, {"HOME": d, "USERPROFILE": d}):
                rc = pe.run(self.args(d))
            files = set(os.listdir(data))
        self.assertEqual(rc, 0)
        self.assertNotIn("sports_home_games.csv", files)
        self.assertIn("business_patterns_county.csv", files)

    def test_missing_earlier_pull_explains(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_it(d), 2)

    def test_steps_argument_limits_work(self):
        urls = []

        def fake(url, *a, **k):
            urls.append(url)
            return self.fake(url, *a, **k)
        with tempfile.TemporaryDirectory() as d:
            self.setup_pull(d)
            with mock.patch.object(pd, "http_get", fake), mock.patch("time.sleep"):
                pe.run(self.args(d, steps="mlb"))
        self.assertTrue(urls and all("statsapi.mlb.com" in u for u in urls))


if __name__ == "__main__":
    unittest.main()
