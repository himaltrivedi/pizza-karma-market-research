"""Offline tests for scripts/pull_data.py. Run: python3 -I -m unittest discover -s tests -v"""
import csv
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import pull_data as pd  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def read_csv(name):
    with open(os.path.join(FIX, name), encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class Names(unittest.TestCase):
    def test_saint_variants(self):
        self.assertEqual(pd.canon("St. Paul"), pd.canon("Saint Paul city"))

    def test_consolidated_governments(self):
        self.assertTrue(pd.canon("Nashville-Davidson metropolitan government (balance)").startswith(pd.canon("Nashville")))
        self.assertTrue(pd.canon("Louisville/Jefferson County metro government (balance)").startswith(pd.canon("Louisville")))
        self.assertEqual(pd.canon("Indianapolis city (balance)"), pd.canon("Indianapolis"))

    def test_clean_place_name(self):
        self.assertEqual(pd.clean_place_name("Chicago city, Illinois"), "Chicago")
        self.assertEqual(pd.clean_place_name("Indianapolis city (balance), Indiana"), "Indianapolis")

    def test_metro_states(self):
        self.assertEqual(pd.metro_states("Chicago-Naperville-Elgin, IL-IN-WI Metro Area"), ["IL", "IN", "WI"])


class CensusKey(unittest.TestCase):
    def test_no_key_raises_without_network(self):
        with mock.patch.object(pd, "http_get", side_effect=AssertionError("must not call network")):
            with self.assertRaises(pd.CensusKeyError):
                pd.census_query(2024, "acs/acs5", ["NAME"], "state:17", None, None)

    def test_html_response_is_key_error(self):
        html = b"<html><head><title>Missing Key</title></head><body>Invalid key</body></html>"
        with mock.patch.object(pd, "http_get", return_value=html):
            with self.assertRaises(pd.CensusKeyError):
                pd.census_query(2024, "acs/acs5", ["NAME"], "state:17", None, "abc")

    def test_key_from_file(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, ".census_key"), "w") as f:
                f.write("filekey\n")
            with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}, clear=False):
                os.environ.pop("CENSUS_API_KEY", None)
                self.assertEqual(pd.get_census_key(), "filekey")
                self.assertEqual(pd.get_census_key("cli"), "cli")

    def test_find_year_does_not_retry_on_key_error(self):
        calls = []

        def fake(*a, **k):
            calls.append(1)
            raise pd.CensusKeyError("x")
        with mock.patch.object(pd, "census_query", fake):
            with self.assertRaises(pd.CensusKeyError):
                pd.find_acs_year("17", "k", dt.date(2026, 11, 2))
        self.assertEqual(len(calls), 1)


def acs_metro(name, cbsa, pop=1000000):
    base = {"NAME": name, "B01003_001E": str(pop), "B19013_001E": "70000", "B01002_001E": "38",
            "B01001_001E": str(pop), "B01003_001M": "500", "B19013_001M": "300",
            "B02018_001E": "5000", "B02018_002E": "4000"}
    for c in pd.AGE_CELLS:
        base[c] = "1000"
    base["metropolitan statistical area/micropolitan statistical area"] = cbsa
    return base


class MetroMatch(unittest.TestCase):
    def run_metro(self, city, st, rows):
        with mock.patch.object(pd, "census_query", return_value=rows):
            return pd.pull_metro(2024, city, "k", st)

    def test_hyphenated_city(self):
        rows = [acs_metro("Winston-Salem, NC Metro Area", "49180"), acs_metro("Salem, OR Metro Area", "41420")]
        got = self.run_metro("Winston-Salem", "NC", rows)
        self.assertEqual([r["cbsa"] for r in got], ["49180"])

    def test_state_disambiguates(self):
        rows = [acs_metro("Portland-Vancouver-Hillsboro, OR-WA Metro Area", "38900"),
                acs_metro("Portland-South Portland, ME Metro Area", "38860")]
        self.assertEqual(self.run_metro("Portland", "ME", rows)[0]["cbsa"], "38860")
        self.assertEqual(self.run_metro("Portland", "OR", rows)[0]["cbsa"], "38900")

    def test_secondary_city_in_title(self):
        rows = [acs_metro("Minneapolis-St. Paul-Bloomington, MN-WI Metro Area", "33460")]
        self.assertEqual(self.run_metro("St. Paul", "MN", rows)[0]["cbsa"], "33460")
        self.assertEqual(self.run_metro("Saint Paul", "MN", rows)[0]["cbsa"], "33460")

    def test_moe_columns(self):
        got = self.run_metro("Chicago", "IL", [acs_metro("Chicago-Naperville-Elgin, IL-IN-WI Metro Area", "16980")])
        self.assertEqual(got[0]["population_moe"], 500)
        self.assertEqual(got[0]["median_hh_income_moe"], 300)


GAZ_PIPE = ("USPS|GEOID|GEOIDFQ|ANSICODE|NAME|LSAD|FUNCSTAT|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG\n"
            "IL|1714000|x|1|Chicago city|25|A|1|1|227.7|1|41.837|-87.685\n"
            "IL|1714026|x|1|Chicago Heights city|25|A|1|1|10.2|1|41.5|-87.6\n"
            "IL|1700001|x|1|Chicago Ridge CDP|57|S|1|1|2.0|1|41.7|-87.7\n"
            "MN|2758000|x|1|St. Paul city|25|A|1|1|52.0|1|44.95|-93.1\n")


class Gazetteer(unittest.TestCase):
    def test_pipe_and_tab(self):
        self.assertEqual(len(pd.parse_gazetteer(GAZ_PIPE)), 4)
        self.assertEqual(len(pd.parse_gazetteer(GAZ_PIPE.replace("|", "\t"))), 4)

    def test_find_principal(self):
        g = pd.parse_gazetteer(GAZ_PIPE)
        self.assertEqual(pd.find_gaz_place(g, "Chicago", "IL"), "1714000")
        self.assertEqual(pd.find_gaz_place(g, "Saint Paul", "MN"), "2758000")
        self.assertIsNone(pd.find_gaz_place(g, "Chicago", "MN"))


class Noaa(unittest.TestCase):
    def test_real_ohare_file_fully_populated(self):
        with open(os.path.join(FIX, "noaa_ohare_raw.csv"), encoding="utf-8") as f:
            rows = pd.tidy_normals(f.read())
        self.assertEqual(len(rows), 12)
        for k in ("avg_high_f", "avg_low_f", "precip_in", "snow_in", "days_high_ge_90f",
                  "days_low_le_32f", "days_precip_ge_0.01in"):
            self.assertTrue(all(r[k] is not None for r in rows), k)
        self.assertAlmostEqual(rows[0]["avg_high_f"], 31.6)
        self.assertEqual(pd.normals_completeness(rows), 12 * 6)


class MetroFilter(unittest.TestCase):
    def test_cbsa_from_principal_city(self):
        sch, col = read_csv("schools_IL_sample.csv"), read_csv("colleges_IL_sample.csv")
        cbsa, how = pd.metro_cbsa({"schools": sch, "colleges": col}, "Chicago", "IL", [])
        self.assertEqual(cbsa, "16980")
        self.assertIn("principal city", how)

    def test_cbsa_prefers_acs_row(self):
        self.assertEqual(pd.metro_cbsa({}, "X", "IL", [{"cbsa": "123"}])[0], "123")

    def test_write_metro_files(self):
        with tempfile.TemporaryDirectory() as d:
            for src, dst in (("schools_IL_sample.csv", "schools_districts_IL.csv"),
                             ("colleges_IL_sample.csv", "colleges_IL.csv")):
                with open(os.path.join(FIX, src), encoding="utf-8") as a, \
                        open(os.path.join(d, dst), "w", encoding="utf-8") as b:
                    b.write(a.read())
            R = pd.Run()
            pd.write_metro_files(d, ["IL"], "Chicago", "IL", [], R)
            got = {f["file"] for f in R.files}
            self.assertEqual(got, {"schools_districts_metro.csv", "colleges_metro.csv", "metro_counties.csv"})
            m = list(csv.DictReader(open(os.path.join(d, "schools_districts_metro.csv"), encoding="utf-8")))
            self.assertTrue(m and all(r["cbsa"] == "16980" for r in m))
            cty = list(csv.DictReader(open(os.path.join(d, "metro_counties.csv"), encoding="utf-8")))
            self.assertIn("17031", {c["county_fips"] for c in cty})


class Socrata(unittest.TestCase):
    def test_where_clause(self):
        self.assertEqual(pd.socrata_where("license_description", ["Mobile Food%", "Special Event Food%"]),
                         "license_description like 'Mobile Food%' OR license_description like 'Special Event Food%'")

    def test_paging(self):
        pages = [[{"a": i} for i in range(5)], [{"a": 9}]]
        urls = []

        def fake(url, *a, **k):
            urls.append(url)
            return json.dumps(pages[len(urls) - 1]).encode()
        with mock.patch.object(pd, "http_get", fake):
            rows = pd.socrata_rows("d.example", "abcd-1234", "f", ["x%"], page=5)
        self.assertEqual(len(rows), 6)
        self.assertIn("$where=", urls[0])
        self.assertIn("offset=5", urls[1])


def fake_internet(url, *a, **k):
    """Minimal fake of every endpoint the script calls."""
    if "api.census.gov" in url:
        q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
        get = q["get"].split(",")
        forv = q["for"]
        if "metropolitan" in forv:
            geo = "metropolitan statistical area/micropolitan statistical area"
            rows = [acs_metro("Chicago-Naperville-Elgin, IL-IN-WI Metro Area", "16980", 9500000)]
            rows[0].pop(geo)
            hdr = get + [geo]
            return json.dumps([hdr] + [[r.get(h, "") for h in get] + ["16980"] for r in rows]).encode()
        st = q.get("in", "state:17").split(":")[1]
        if forv.startswith("state"):
            return json.dumps([["NAME", "state"], ["Illinois", "17"]]).encode()
        key_col = "place" if forv.startswith("place") else "county"
        if "P1_001N" in get:
            return json.dumps([["NAME", "P1_001N", "state", "place"], ["Chicago city, Illinois", "2746388", st, "14000"]]).encode()
        names = [("Chicago city, Illinois", "14000", 2700000), ("Naperville city, Illinois", "51622", 150000)] \
            if key_col == "place" else [("Cook County, Illinois", "031", 5200000)]
        out = [get + ["state", key_col]]
        for nm, code, pop in names:
            r = acs_metro(nm, "x", pop)
            out.append([r.get(h, "") for h in get] + [st, code])
        return json.dumps(out).encode()
    if "Gaz_place" in url:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("g.txt", GAZ_PIPE.replace("1714000", "1714000"))
        return buf.getvalue()
    if "ghcnd-stations" in url:
        return b"USW00094846  41.9950  -87.9336  201.8 IL CHICAGO OHARE INTL AP                  GSN     HCN 72530\n"
    if "normals-monthly" in url:
        return open(os.path.join(FIX, "noaa_ohare_raw.csv"), "rb").read()
    if "educationdata.urban.org" in url:
        name = "colleges_IL_sample.csv" if "college" in url else "schools_IL_sample.csv"
        return json.dumps({"results": read_csv(name), "next": None}).encode()
    if "cityofchicago" in url:
        return json.dumps([{"license_description": "Mobile Food License", "status": "AAI"}]).encode()
    if "overpass-api" in url:
        els = [{"type": "count", "id": 0, "tags": {"nodes": "0", "ways": "0", "relations": "0", "total": str(i + 1)}}
               for i in range(7)]
        return json.dumps({"elements": els}).encode()
    raise AssertionError("unexpected url " + url)


class EndToEnd(unittest.TestCase):
    def run_pull(self, key):
        with tempfile.TemporaryDirectory() as d:
            args = pd.argparse.Namespace(
                city="Chicago, IL", places="Naperville", top=40, states="IL", station=None, outdir=d,
                census_key=key, no_schools=False, socrata_domain=None, socrata_dataset=None,
                socrata_field=None, socrata_search=None, selftest=False, radius=35.0)
            with mock.patch.object(pd, "http_get", fake_internet), \
                    mock.patch.dict(os.environ, {"HOME": d, "USERPROFILE": d}), \
                    mock.patch("time.sleep"):
                os.environ.pop("CENSUS_API_KEY", None)
                rc = pd.run(args)
            data = os.path.join(d, "data")
            files = set(os.listdir(data))
            manifest = open(os.path.join(data, "MANIFEST.md"), encoding="utf-8").read()
            core = None
            if "acs_places_core.csv" in files:
                core = list(csv.DictReader(open(os.path.join(data, "acs_places_core.csv"), encoding="utf-8")))
            return rc, files, manifest, core

    def test_with_key(self):
        rc, files, manifest, core = self.run_pull("testkey")
        self.assertEqual(rc, 0)
        for f in ("acs_metro.csv", "acs_places_core.csv", "acs_counties_IL.csv", "noaa_monthly_normals.csv",
                  "schools_districts_metro.csv", "colleges_metro.csv", "metro_counties.csv",
                  "chicago_business_licenses_mobile_food_rows.csv", "MANIFEST.md"):
            self.assertIn(f, files)
        self.assertEqual([r["name"] for r in core], ["Chicago", "Naperville"])
        self.assertTrue(core[0]["density_per_sqmi"])        # joined to the Gazetteer
        self.assertIn("population_moe", core[0])
        self.assertIn("USW00094846", manifest)

    def test_without_key_still_gets_weather_and_schools(self):
        rc, files, manifest, _ = self.run_pull(None)
        self.assertNotIn("acs_metro.csv", files)
        self.assertIn("noaa_monthly_normals.csv", files)
        self.assertIn("schools_districts_metro.csv", files)
        self.assertIn("No usable Census API key", manifest)


if __name__ == "__main__":
    unittest.main()


class ExtraChecks(unittest.TestCase):
    def test_pop_change_note_flags_big_swings(self):
        def census(year, dataset, get, for_, in_, key):
            if "B01003_001E" in get and len(get) == 2:           # earlier-period population
                return [{"NAME": "A city, Illinois", "B01003_001E": "200000", "state": "17", "place": "00001"}]
            r = acs_metro("A city, Illinois", "x", 180000)
            r.update({"state": "17", "place": "00001"})
            return [r]
        with mock.patch.object(pd, "census_query", census):
            rows = pd.pull_places(2024, "IL", "k", None)
        self.assertEqual(rows[0]["pop_change_pct"], -10.0)
        self.assertTrue(rows[0]["pop_change_note"].startswith("CHECK"))

    def test_metro_counties_has_state(self):
        with tempfile.TemporaryDirectory() as d:
            for src, dst in (("schools_IL_sample.csv", "schools_districts_IL.csv"),
                             ("colleges_IL_sample.csv", "colleges_IL.csv")):
                with open(os.path.join(FIX, src), encoding="utf-8") as a, \
                        open(os.path.join(d, dst), "w", encoding="utf-8") as b:
                    b.write(a.read())
            pd.write_metro_files(d, ["IL"], "Chicago", "IL", [], pd.Run())
            with open(os.path.join(d, "metro_counties.csv"), encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertTrue(all(r["state"] == "IL" for r in rows))


class UrbanFailFast(unittest.TestCase):
    def test_timeout_stops_year_probing(self):
        calls = []

        def boom(url, *a, **k):
            calls.append(url)
            raise pd.NetworkError("timed out")
        with mock.patch.object(pd, "http_get", boom):
            with self.assertRaises(pd.NetworkError):
                pd.urban_latest("school-districts/ccd/directory/{year}/", "47", dt.date(2026, 11, 3))
        self.assertEqual(len(calls), 1)

    def test_http_404_tries_earlier_year(self):
        seen = []

        def fake(url, *a, **k):
            seen.append(url)
            if "/2024/" not in url:
                raise pd.HttpError(404, "not published")
            return json.dumps({"results": [{"leaid": "1"}], "next": None}).encode()
        with mock.patch.object(pd, "http_get", fake):
            rows, y = pd.urban_latest("school-districts/ccd/directory/{year}/", "47", dt.date(2026, 11, 3))
        self.assertEqual(y, 2024)
        self.assertEqual(len(seen), 1 if "/2024/" in seen[0] else 2)

    def test_run_skips_remaining_urban_calls_after_timeout(self):
        urban_calls = []

        def fake(url, *a, **k):
            if "educationdata.urban.org" in url:
                urban_calls.append(url)
                raise pd.NetworkError("timed out")
            return fake_internet(url, *a, **k)
        with tempfile.TemporaryDirectory() as d:
            args = pd.argparse.Namespace(
                city="Chicago, IL", places="Naperville", top=40, states="IL,IN", station=None, outdir=d,
                census_key="k", no_schools=False, socrata_domain=None, socrata_dataset=None,
                socrata_field=None, socrata_search=None, selftest=False, radius=35.0)
            with mock.patch.object(pd, "http_get", fake), mock.patch("time.sleep"):
                pd.run(args)
            manifest = open(os.path.join(d, "data", "MANIFEST.md"), encoding="utf-8").read()
        self.assertEqual(len(urban_calls), 1)                      # one try, then everything else skipped
        self.assertIn("skipped: the Urban Institute API was unreachable", manifest)
        self.assertIn("noaa_monthly_normals.csv", manifest)       # other sources unaffected


class PoiAndRadius(unittest.TestCase):
    def test_query_has_one_count_per_set(self):
        q = pd.poi_query(41.8, -87.6, 5000)
        self.assertEqual(q.count("out count;"), len(pd.POI_SETS))
        self.assertIn("around:5000,41.80000,-87.60000", q)

    def test_one_fixed_radius_for_every_place(self):
        self.assertEqual(pd.POI_RADIUS_M, 5000)

    def test_parse_poi(self):
        els = [{"type": "count", "tags": {"total": str(i * 10)}} for i in range(len(pd.POI_SETS))]
        got = pd.parse_poi(json.dumps({"elements": els}))
        self.assertEqual(got["breweries"], 0)
        self.assertEqual(got["restaurants"], 20)
        with self.assertRaises(RuntimeError):
            pd.parse_poi(json.dumps({"elements": els[:3]}))

    def test_default_core_list_stays_near_principal_city(self):
        def place(geoid, name, pop, lat, lon):
            return {"geoid": geoid, "name": name, "state": "TX", "population": pop, "lat": lat, "lon": lon}
        austin = place("1", "Austin", 900000, 30.27, -97.74)
        rows = [austin, place("2", "Houston", 2300000, 29.76, -95.37), place("3", "Round Rock", 120000, 30.51, -97.68),
                place("4", "Cedar Park", 80000, 30.51, -97.82)]
        core, _ = pd.pick_core(rows, austin, [], 5, 35.0)
        self.assertEqual([r["name"] for r in core], ["Austin", "Round Rock", "Cedar Park"])
        core_all, _ = pd.pick_core(rows, austin, [], 5, None)
        self.assertIn("Houston", [r["name"] for r in core_all])

    def test_slash_in_metro_title(self):
        rows = [acs_metro("Louisville/Jefferson County, KY-IN Metro Area", "31140")]
        with mock.patch.object(pd, "census_query", return_value=rows):
            got = pd.pull_metro(2024, "Louisville", "k", "KY")
        self.assertEqual([r["cbsa"] for r in got], ["31140"])



def overpass_ok():
    return fake_internet("https://overpass-api.de/api/interpreter?data=x")


PLACES = [{"name": "A", "state": "IL", "lat": 41.8, "lon": -87.6, "land_sqmi": 20.0},
          {"name": "NoCoords", "state": "IL", "lat": None, "lon": None, "land_sqmi": 5.0},
          {"name": "B", "state": "IL", "lat": 41.9, "lon": -87.7, "land_sqmi": 12.0}]


class OverpassResilience(unittest.TestCase):
    def read(self, path):
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def test_backoff_and_mirror_alternation(self):
        urls = []

        def fake(url, *a, **k):
            urls.append(url)
            if len(urls) < 3:
                raise pd.HttpError(502, "at capacity")
            return overpass_ok()
        with mock.patch.object(pd, "http_get", fake), mock.patch("time.sleep"):
            got = pd.overpass_counts(41.8, -87.6, 5000, waits=(0, 1, 2, 3))
        self.assertEqual(got["breweries"], 1)
        self.assertEqual(len(urls), 3)
        self.assertIn("overpass-api.de", urls[0])
        self.assertIn("kumi", urls[1])
        self.assertIn("private.coffee", urls[2])

    def test_gives_up_after_all_tries(self):
        with mock.patch.object(pd, "http_get", side_effect=pd.HttpError(502, "busy")), mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                pd.overpass_counts(41.8, -87.6, 5000, waits=(0, 1))

    def test_pull_poi_writes_rows_skips_no_coordinates(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(pd, "http_get", lambda *a, **k: overpass_ok()), \
                mock.patch("time.sleep"):
            path = os.path.join(d, "poi.csv")
            n, errs = pd.pull_poi(PLACES, path)
            rows = self.read(path)
        self.assertEqual((n, errs), (2, []))
        self.assertEqual(sorted(r["name"] for r in rows), ["A", "B"])
        self.assertTrue(all(r["radius_m"] == "5000" for r in rows))

    def test_stops_after_three_failures_and_keeps_partial_rows(self):
        def fake(url, *a, **k):
            if "place" in url and "%22place%22" in url:        # center lookup: nothing found, fall back
                return json.dumps({"elements": []}).encode()
            if "P0" in fake.current:
                return overpass_ok()
            raise pd.HttpError(502, "busy")
        fake.current = ""
        places = [dict(PLACES[0], name=f"P{i}") for i in range(6)]
        seen = []

        def counting(url, *a, **k):
            seen.append(url)
            # first count query succeeds, everything after fails
            if "%22place%22" in url:
                return json.dumps({"elements": []}).encode()
            if sum(1 for u in seen if "%22place%22" not in u) == 1:
                return overpass_ok()
            raise pd.HttpError(502, "busy")
        with tempfile.TemporaryDirectory() as d, mock.patch.object(pd, "http_get", counting), mock.patch("time.sleep"):
            path = os.path.join(d, "poi.csv")
            n, errs = pd.pull_poi(places, path, waits=(0,), workers=1)
            rows = self.read(path)
        self.assertEqual(n, 1)
        self.assertEqual(rows[0]["center"], "census internal point")
        self.assertTrue(any("stopped after 3 failures" in e for e in errs))

    def test_max_places(self):
        places = [dict(PLACES[0], name=f"P{i}") for i in range(6)]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(pd, "http_get", lambda *a, **k: overpass_ok()), \
                mock.patch("time.sleep"):
            n, _ = pd.pull_poi(places, os.path.join(d, "p.csv"), max_places=3)
        self.assertEqual(n, 3)

    def test_poi_only_adds_to_existing_pull_and_manifest(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(pd, "http_get", lambda *a, **k: overpass_ok()), \
                mock.patch("time.sleep"):
            data = os.path.join(d, "data")
            os.makedirs(data)
            with open(os.path.join(data, "acs_places_core.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["name", "state", "lat", "lon", "land_sqmi"])
                w.writeheader()
                w.writerow({"name": "Chicago", "state": "IL", "lat": 41.8, "lon": -87.6, "land_sqmi": 227})
            with open(os.path.join(data, "MANIFEST.md"), "w", encoding="utf-8") as f:
                f.write("# existing manifest\n")
            rc = pd.poi_only(d)
            manifest = open(os.path.join(data, "MANIFEST.md"), encoding="utf-8").read()
            rows = self.read(os.path.join(data, "poi_counts_osm.csv"))
        self.assertEqual(rc, 0)
        self.assertEqual(rows[0]["name"], "Chicago")
        self.assertTrue(manifest.startswith("# existing manifest"))
        self.assertIn("poi_counts_osm.csv", manifest)

    def test_poi_only_without_core_file_explains(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pd.poi_only(d), 2)

    def test_main_pull_never_calls_overpass(self):
        called = []

        def fake(url, *a, **k):
            called.append("overpass" in url)
            return fake_internet(url, *a, **k)
        with tempfile.TemporaryDirectory() as d:
            args = pd.argparse.Namespace(
                city="Chicago, IL", places="Naperville", top=40, states="IL", station=None, outdir=d,
                census_key="k", no_schools=True, socrata_domain=None, socrata_dataset=None,
                socrata_field=None, socrata_search=None, selftest=False, radius=35.0)
            with mock.patch.object(pd, "http_get", fake), mock.patch("time.sleep"):
                pd.run(args)
        self.assertFalse(any(called))


class StationWindow(unittest.TestCase):
    def test_far_station_is_not_chosen_for_completeness(self):
        def line(sid, lat, lon, st, name):
            return f"{sid:<11} {lat:>8.4f} {lon:>9.4f} {10.0:>6.1f} {st:<2} {name:<30} GSN     HCN 11111"
        stations = "\n".join([line("USW00000001", 43.6, -70.3, "ME", "PORTLAND INTL JETPORT"),
                              line("USW00000002", 43.96, -69.7, "ME", "WISCASSET AP")])
        cands = pd.nearest_airport_stations(pd.parse_ghcnd_stations(stations), 43.66, -70.26)
        self.assertEqual(cands[0]["name"], "PORTLAND INTL JETPORT")
        self.assertGreater(cands[1]["miles"], cands[0]["miles"] + pd.STATION_WINDOW_MILES)


class StationNames(unittest.TestCase):
    def test_jetport_accepted_and_offset_coop_sites_rejected(self):
        def line(sid, lat, lon, st, name):
            return f"{sid:<11} {lat:>8.4f} {lon:>9.4f} {10.0:>6.1f} {st:<2} {name:<30} GSN     HCN 11111"
        txt = "\n".join([line("USW00014764", 43.65, -70.30, "ME", "PORTLAND JETPORT"),
                        line("USW00099999", 43.70, -70.30, "ME", "LIMESTONE 4 NNW"),
                        line("USC00000001", 43.66, -70.26, "ME", "PORTLAND COOP")])
        got = pd.nearest_airport_stations(pd.parse_ghcnd_stations(txt), 43.66, -70.26)
        self.assertEqual([c["name"] for c in got], ["PORTLAND JETPORT"])


class CenterLookup(unittest.TestCase):
    def test_search_name(self):
        self.assertEqual(pd.place_search_name("Nashville-Davidson metropolitan government"), "Nashville")
        self.assertEqual(pd.place_search_name("Louisville/Jefferson County metro government"), "Louisville")
        self.assertEqual(pd.place_search_name("Oak Park"), "Oak Park")
        self.assertEqual(pd.place_search_name("St. Paul"), "St. Paul")

    def test_center_query_escapes_regex(self):
        q = pd.center_query(43.6, -70.2, "St. Paul")
        self.assertIn('"^St. Paul$"', q)
        self.assertNotIn(chr(92), q)
        self.assertIn('"place"~"^(city|town|village)$"', q)

    def test_parse_center_prefers_city_then_nearest(self):
        els = [{"type": "node", "lat": 43.1, "lon": -70.1, "tags": {"place": "village"}},
               {"type": "node", "lat": 43.66, "lon": -70.25, "tags": {"place": "city"}},
               {"type": "node", "lat": 43.9, "lon": -70.0, "tags": {"place": "city"}}]
        self.assertEqual(pd.parse_center(json.dumps({"elements": els}), 43.63, -70.18), (43.66, -70.25))
        self.assertIsNone(pd.parse_center(json.dumps({"elements": []}), 1, 1))

    def test_find_center_uses_osm_node_else_census_point(self):
        node = {"elements": [{"type": "node", "lat": 43.66, "lon": -70.25, "tags": {"place": "city"}}]}
        pl = {"name": "Portland", "lat": 43.63, "lon": -70.18}
        with mock.patch.object(pd, "http_get", lambda *a, **k: json.dumps(node).encode()):
            self.assertEqual(pd.find_center(pl, (0, 1)), (43.66, -70.25, "osm place node"))
        with mock.patch.object(pd, "http_get", side_effect=pd.HttpError(502, "busy")), mock.patch("time.sleep"):
            self.assertEqual(pd.find_center(pl, (0, 1)), (43.63, -70.18, "census internal point"))


class PopChange(unittest.TestCase):
    def test_small_change_no_note(self):
        self.assertEqual(pd.pop_change_note(3.0, 100, 100), "")
        self.assertEqual(pd.pop_change_note(None, None, None), "")

    def test_base_off_from_census_means_revision(self):
        n = pd.pop_change_note(-10.0, 199927, 180542)
        self.assertTrue(n.startswith("CHECK") and "change_vs_2020_pct" in n)

    def test_base_agrees_with_census_means_real(self):
        n = pd.pop_change_note(18.6, 24000, 24500)
        self.assertIn("probably real", n)

    def test_no_census_count(self):
        self.assertTrue(pd.pop_change_note(9.0, 100, None).startswith("CHECK: over 5%"))

    def test_pull_places_adds_2020_columns(self):
        def census(year, dataset, get, for_, in_, key):
            if dataset == "dec/pl":
                return [{"NAME": "A city, Illinois", "P1_001N": "180542", "state": "17", "place": "00001"}]
            if len(get) == 2 and "B01003_001E" in get:
                return [{"NAME": "A city, Illinois", "B01003_001E": "199927", "state": "17", "place": "00001"}]
            r = acs_metro("A city, Illinois", "x", 179898)
            r.update({"state": "17", "place": "00001"})
            return [r]
        with mock.patch.object(pd, "census_query", census):
            rows = pd.pull_places(2024, "IL", "k", None)
        r = rows[0]
        self.assertEqual(r["census2020_pop"], 180542)
        self.assertEqual(r["change_vs_2020_pct"], -0.4)
        self.assertTrue(r["pop_change_note"].startswith("CHECK"))


class PartialFileName(unittest.TestCase):
    def test_final_name_appears_only_when_finished(self):
        seen = []

        def fake(url, *a, **k):
            seen.append((os.path.exists(os.path.join(d, "poi.csv")), os.path.exists(os.path.join(d, "poi.csv.partial"))))
            return overpass_ok()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(pd, "http_get", fake), mock.patch("time.sleep"):
            pd.pull_poi([dict(PLACES[0])], os.path.join(d, "poi.csv"), workers=1)
            final = os.path.exists(os.path.join(d, "poi.csv"))
            leftover = os.path.exists(os.path.join(d, "poi.csv.partial"))
        self.assertTrue(final and not leftover)
        self.assertTrue(all(not done for done, _ in seen))       # never visible as finished mid-run


class ChooseStation(unittest.TestCase):
    """The Chicago case: non-airport sites are nearest, Midway lacks snow, O'Hare is complete."""
    def cands(self):
        return [{"id": "USW00014881", "name": "FORT DEARBORN", "miles": 4.7},
                {"id": "USW00014819", "name": "CHICAGO MIDWAY AP", "miles": 5.1},
                {"id": "USW00014892", "name": "CHICAGO UNIV", "miles": 5.7},
                {"id": "USW00014810", "name": "PARK RIDGE", "miles": 14.6},
                {"id": "USW00094846", "name": "CHICAGO OHARE INTL AP", "miles": 15.3},
                {"id": "USW00014855", "name": "GLENVIEW NAS", "miles": 18.7},
                {"id": "USW00004838", "name": "CHICAGO PALWAUKEE AP", "miles": 22.6}]

    def fetcher(self, files):
        def fetch(sid):
            if sid not in files:
                raise RuntimeError("404")
            return files[sid]
        return fetch

    def raw(self, snow=True):
        with open(os.path.join(FIX, "noaa_ohare_raw.csv"), encoding="utf-8") as f:
            return f.read()

    def test_o_hare_beats_nearer_sites_that_lack_snow(self):
        full = self.raw(True)
        with open(os.path.join(FIX, "noaa_ohare_raw.csv"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        import csv as _csv
        import io as _io
        rows = list(_csv.reader(_io.StringIO("\n".join(lines))))
        snowless = [[c for i, c in enumerate(r) if "SNOW" not in rows[0][i]] for r in rows]
        buf = _io.StringIO()
        _csv.writer(buf).writerows(snowless)
        no_snow = buf.getvalue()
        files = {"USW00014881": no_snow, "USW00014819": no_snow, "USW00014892": no_snow, "USW00094846": full}
        got = pd.choose_station(self.cands(), self.fetcher(files))
        self.assertEqual(got["id"], "USW00094846")
        self.assertIn("CHICAGO OHARE INTL AP", got["label"])

    def test_tie_goes_to_airport_then_nearest(self):
        full = self.raw(True)
        files = {"USW00014881": full, "USW00014819": full, "USW00094846": full}
        got = pd.choose_station(self.cands(), self.fetcher(files))
        self.assertEqual(got["id"], "USW00014819")                  # Midway: airport and nearer than O'Hare

    def test_far_station_outside_window_ignored(self):
        full = self.raw(True)
        files = {"USW00014881": "month\n1\n", "USW00004838": full}   # Palwaukee is 22.6 mi: beyond 4.7 + 12
        got = pd.choose_station(self.cands(), self.fetcher(files))
        self.assertEqual(got["id"], "USW00014881")

    def test_nothing_fetchable(self):
        self.assertIsNone(pd.choose_station(self.cands(), self.fetcher({})))
        self.assertIsNone(pd.choose_station([], self.fetcher({})))


class RunStamp(unittest.TestCase):
    def folder(self, d, with_sections=True, run=None):
        out = os.path.join(d, "reports", "chicago-il")
        os.makedirs(os.path.join(out, "sections"))
        if with_sections:
            with open(os.path.join(out, "sections", "00-brief.md"), "w") as f:
                f.write("## Executive brief\n")
            with open(os.path.join(out, "report.md"), "w") as f:
                f.write("x")
        if run is not None:
            with open(os.path.join(out, "run.json"), "w") as f:
                json.dump(run, f)
        return out

    def test_skill_version_changes_with_the_rules(self):
        with tempfile.TemporaryDirectory() as d:
            for n in pd.SKILL_FILES:
                with open(os.path.join(d, n), "w") as f:
                    f.write("v1")
            v1 = pd.skill_version(d)
            self.assertEqual(v1, pd.skill_version(d))
            with open(os.path.join(d, "modules.md"), "w") as f:
                f.write("v2")
            self.assertNotEqual(v1, pd.skill_version(d))

    def test_fresh_folder_just_gets_a_stamp(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r")
            got = pd.start_run(out, "Chicago, IL", dt.date(2026, 11, 3), "abc")
            self.assertFalse(got["archived"] or got["resumed"])
            self.assertEqual(json.load(open(os.path.join(out, "run.json")))["started"], "2026-11-03")

    def test_same_version_same_week_resumes_and_keeps_start_date(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.folder(d, run={"started": "2026-11-02", "skill_version": "abc"})
            got = pd.start_run(out, "Chicago, IL", dt.date(2026, 11, 3), "abc")
            self.assertTrue(got["resumed"])
            self.assertEqual(got["info"]["started"], "2026-11-02")
            self.assertTrue(os.path.exists(os.path.join(out, "sections", "00-brief.md")))

    def test_changed_skill_archives_old_sections(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.folder(d, run={"started": "2026-11-03", "skill_version": "old"})
            got = pd.start_run(out, "Chicago, IL", dt.date(2026, 11, 3), "new")
            self.assertIn("skill changed", got["reason"])
            self.assertFalse(os.path.exists(os.path.join(out, "sections")))
            self.assertTrue(os.path.exists(os.path.join(got["archived"], "sections", "00-brief.md")))
            self.assertTrue(os.path.exists(os.path.join(got["archived"], "report.md")))
            self.assertEqual(json.load(open(os.path.join(out, "run.json")))["started"], "2026-11-03")

    def test_old_run_and_unknown_run_are_archived(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.folder(d, run={"started": "2026-10-27", "skill_version": "abc"})
            got = pd.start_run(out, "Chicago, IL", dt.date(2026, 11, 3), "abc")
            self.assertIn("days old", got["reason"])
        with tempfile.TemporaryDirectory() as d:
            out = self.folder(d, run=None)
            got = pd.start_run(out, "Chicago, IL", dt.date(2026, 11, 3), "abc")
            self.assertIn("unknown earlier run", got["reason"])

    def test_run_archives_before_pulling(self):
        with tempfile.TemporaryDirectory() as d:
            out = self.folder(d, run={"started": "2026-10-27", "skill_version": "x"})
            args = pd.argparse.Namespace(
                city="Chicago, IL", places="Naperville", top=40, states="IL", station=None, outdir=out,
                census_key="k", no_schools=True, socrata_domain=None, socrata_dataset=None,
                socrata_field=None, socrata_search=None, selftest=False, radius=35.0)
            with mock.patch.object(pd, "http_get", fake_internet), mock.patch("time.sleep"):
                pd.run(args)
            self.assertFalse(os.path.exists(os.path.join(out, "sections")))
            self.assertTrue(os.listdir(os.path.join(out, "sections_archive")))
            self.assertTrue(os.path.exists(os.path.join(out, "run.json")))


class ManualFolder(unittest.TestCase):
    def test_manual_inbox_is_created_with_instructions(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r")
            pd.start_run(out, "Chicago, IL", dt.date(2026, 11, 3), "abc")
            readme = open(os.path.join(out, "manual", "README.txt"), encoding="utf-8").read()
            self.assertIn("Save Page As", readme)
            open(os.path.join(out, "manual", "page.html"), "w").write("x")
            pd.start_run(out, "Chicago, IL", dt.date(2026, 11, 3), "abc")      # second call keeps the owner's files
            self.assertTrue(os.path.exists(os.path.join(out, "manual", "page.html")))


class NeighborhoodsAndRetry(unittest.TestCase):
    def els(self):
        return json.dumps({"elements": [
            {"type": "node", "lat": 41.88, "lon": -87.63, "tags": {"name": "Loop", "place": "suburb"}},
            {"type": "node", "lat": 41.88, "lon": -87.66, "tags": {"name": "West Loop", "place": "neighbourhood"}},
            {"type": "node", "lat": 41.88, "lon": -87.66, "tags": {"name": "West Loop", "place": "suburb"}},   # better kind wins
            {"type": "node", "lat": 41.91, "lon": -87.68, "tags": {"name": "Wicker Park", "place": "quarter"}},
            {"type": "node", "lat": 41.9, "lon": -87.7, "tags": {"place": "suburb"}},                          # no name: dropped
            {"type": "way", "tags": {"name": "A way", "place": "suburb"}}]}).encode()

    def test_parse_dedupes_ranks_and_caps(self):
        got = pd.parse_neighborhoods(self.els(), cap=2)
        self.assertEqual([g["name"] for g in got], ["Loop", "West Loop"])
        self.assertEqual(got[1]["place"], "suburb")
        self.assertEqual(len(pd.parse_neighborhoods(self.els(), cap=25)), 3)

    def test_center_keeps_the_core_districts_when_capped(self):
        # Alphabetical order would keep "Albany Park" and "Beverly" and drop the Loop: distance order keeps the Loop.
        els = json.dumps({"elements": [
            {"type": "node", "lat": 41.97, "lon": -87.72, "tags": {"name": "Albany Park", "place": "suburb"}},
            {"type": "node", "lat": 41.72, "lon": -87.67, "tags": {"name": "Beverly", "place": "suburb"}},
            {"type": "node", "lat": 41.88, "lon": -87.63, "tags": {"name": "Loop", "place": "suburb"}},
            {"type": "node", "lat": 41.89, "lon": -87.64, "tags": {"name": "River North", "place": "quarter"}}]}).encode()
        got = pd.parse_neighborhoods(els, cap=2, center=(41.8781, -87.6298))
        self.assertEqual([g["name"] for g in got], ["Loop", "River North"])
        old = pd.parse_neighborhoods(els, cap=2)
        self.assertEqual([g["name"] for g in old], ["Albany Park", "Beverly"])

    def test_query_with_names_is_regex_safe(self):
        q = pd.neighborhood_query(41.8, -87.6, 9000, ["Loop", "St. Paul's"])
        self.assertIn('^(Loop|St. Paul.s)$', q)
        self.assertNotIn(chr(92), q)
        self.assertNotIn('["name"~', pd.neighborhood_query(41.8, -87.6, 9000))

    def test_neighborhoods_only_end_to_end_without_recentering(self):
        calls = []

        def fake(url, *a, **k):
            calls.append(url)
            if "%22suburb%7Cquarter%7Cneighbourhood%22" in url or "suburb" in url:
                return self.els()
            return overpass_ok()
        with tempfile.TemporaryDirectory() as d:
            data = os.path.join(d, "data")
            os.makedirs(data)
            with open(os.path.join(data, "acs_places_core.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["name", "state", "lat", "lon", "land_sqmi"])
                w.writeheader()
                w.writerow({"name": "Chicago", "state": "IL", "lat": 41.8, "lon": -87.6, "land_sqmi": 227})
            open(os.path.join(data, "MANIFEST.md"), "w").write("# m\n")
            with mock.patch.object(pd, "http_get", fake), mock.patch("time.sleep"):
                rc = pd.neighborhoods_only(d, cap=25)
            rows = list(csv.DictReader(open(os.path.join(data, "neighborhood_counts_osm.csv"), encoding="utf-8")))
            manifest = open(os.path.join(data, "MANIFEST.md"), encoding="utf-8").read()
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(r["name"] for r in rows), ["Loop", "West Loop", "Wicker Park"])
        self.assertTrue(all(r["radius_m"] == "1500" and r["center"] == "given point" for r in rows))
        self.assertIn("Neighborhood counts", manifest)
        self.assertFalse(any("place%22" in u and "around%3A1500" in u for u in calls))      # no per-hood center lookups

    def test_neighborhoods_only_needs_the_earlier_pull(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pd.neighborhoods_only(d), 2)

    def test_failed_place_is_retried_once_at_the_end(self):
        state = {"calls": 0}

        def fake(url, *a, **k):
            if "%22place%22" in url:
                return json.dumps({"elements": []}).encode()
            state["calls"] += 1
            if state["calls"] <= 5:                      # the first place's 5 tries all fail
                raise pd.HttpError(502, "busy")
            return overpass_ok()
        places = [dict(PLACES[0], name="A"), dict(PLACES[2], name="B")]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(pd, "http_get", fake), mock.patch("time.sleep"):
            n, errs = pd.pull_poi(places, os.path.join(d, "p.csv"), workers=1, max_failures=3)
            names = sorted(r["name"] for r in csv.DictReader(open(os.path.join(d, "p.csv"), encoding="utf-8")))
        self.assertEqual(names, ["A", "B"])
        self.assertEqual(n, 2)
        self.assertEqual(errs, [])                        # the retry succeeded, so no problem is reported
