#!/usr/bin/env python3
"""
pull_data.py - pull public data for one city so the research can start from
real files instead of search snippets.

Standard library only (Python 3.9+). No installs needed.

Usage
  python3 pull_data.py "Chicago, IL" --places "Naperville, Evanston, Skokie"
  python3 pull_data.py "Chicago, IL" --outdir reports/chicago-il
  python3 pull_data.py --selftest        # checks that every data source is reachable

What it writes (into <outdir>/data/):
  acs_metro.csv              Census ACS 5-year totals for the metro area
  acs_places_core.csv        the principal city + the core suburbs you name (or the largest places)
  acs_places_all_<ST>.csv    every place in each metro state (reference list)
  acs_counties_<ST>.csv      every county in each metro state
  noaa_monthly_normals.csv   monthly climate normals (1991-2020) for the nearest airport station
  noaa_normals_raw.csv       the untouched NOAA file
  schools_districts_<ST>.csv public school districts (NCES Common Core, via Urban Institute API)
  colleges_<ST>.csv          colleges and universities (IPEDS directory, via Urban Institute API)
  *_license_counts.csv       city open-data license counts by type (Chicago preset or --socrata-*)
  MANIFEST.md                what was pulled, from where, which year, and what failed
  pull_log.json              machine-readable log of every step

Every step is independent: if one source fails the rest still run, and the
failure is written to MANIFEST.md so the report can say so.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any, Dict, List, Optional, Tuple

UA = "pizza-karma-market-research/1.0 (public-data pull script)"
KEY_HELP = ("Census API key missing or invalid. Get a free key at "
            "https://api.census.gov/data/key_signup.html and set CENSUS_API_KEY "
            "(or pass --census-key, or save it in ~/.census_key)")


class NetworkError(RuntimeError):
    """The server could not be reached or timed out (as opposed to answering with an HTTP error)."""


class HttpError(RuntimeError):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


class CensusKeyError(RuntimeError):
    """The Census API refused the request because the key is missing or invalid."""


def get_census_key(cli_key: Optional[str] = None) -> Optional[str]:
    key = cli_key or os.environ.get("CENSUS_API_KEY")
    if not key:
        path = os.path.join(os.path.expanduser("~"), ".census_key")
        try:
            with open(path, encoding="utf-8") as f:
                key = f.read().strip() or None
        except OSError:
            key = None
    return key.strip() if key else None
TIMEOUT = 90

STATES: Dict[str, Tuple[str, str]] = {
    "AL": ("01", "Alabama"), "AK": ("02", "Alaska"), "AZ": ("04", "Arizona"),
    "AR": ("05", "Arkansas"), "CA": ("06", "California"), "CO": ("08", "Colorado"),
    "CT": ("09", "Connecticut"), "DE": ("10", "Delaware"),
    "DC": ("11", "District of Columbia"), "FL": ("12", "Florida"),
    "GA": ("13", "Georgia"), "HI": ("15", "Hawaii"), "ID": ("16", "Idaho"),
    "IL": ("17", "Illinois"), "IN": ("18", "Indiana"), "IA": ("19", "Iowa"),
    "KS": ("20", "Kansas"), "KY": ("21", "Kentucky"), "LA": ("22", "Louisiana"),
    "ME": ("23", "Maine"), "MD": ("24", "Maryland"), "MA": ("25", "Massachusetts"),
    "MI": ("26", "Michigan"), "MN": ("27", "Minnesota"), "MS": ("28", "Mississippi"),
    "MO": ("29", "Missouri"), "MT": ("30", "Montana"), "NE": ("31", "Nebraska"),
    "NV": ("32", "Nevada"), "NH": ("33", "New Hampshire"), "NJ": ("34", "New Jersey"),
    "NM": ("35", "New Mexico"), "NY": ("36", "New York"), "NC": ("37", "North Carolina"),
    "ND": ("38", "North Dakota"), "OH": ("39", "Ohio"), "OK": ("40", "Oklahoma"),
    "OR": ("41", "Oregon"), "PA": ("42", "Pennsylvania"), "RI": ("44", "Rhode Island"),
    "SC": ("45", "South Carolina"), "SD": ("46", "South Dakota"),
    "TN": ("47", "Tennessee"), "TX": ("48", "Texas"), "UT": ("49", "Utah"),
    "VT": ("50", "Vermont"), "VA": ("51", "Virginia"), "WA": ("53", "Washington"),
    "WV": ("54", "West Virginia"), "WI": ("55", "Wisconsin"), "WY": ("56", "Wyoming"),
}
ABBR_BY_NAME = {v[1].lower(): k for k, v in STATES.items()}

# ACS 5-year variables
# B01001: sex by age. Ages 18-34 = male cells 007-012 and female cells 031-036.
AGE_CELLS = [
    "B01001_007E", "B01001_008E", "B01001_009E", "B01001_010E", "B01001_011E", "B01001_012E",
    "B01001_031E", "B01001_032E", "B01001_033E", "B01001_034E", "B01001_035E", "B01001_036E",
]
CORE_VARS = ["B01003_001E", "B19013_001E", "B01002_001E", "B01001_001E",
             "B01003_001M", "B19013_001M"] + AGE_CELLS
# B02018: Asian alone or in any combination, by selected groups (001 = total, 002 = Asian Indian)
ASIAN_VARS = ["B02018_001E", "B02018_002E"]

CENSUS = "https://api.census.gov/data"
URBAN = "https://educationdata.urban.org/api/v1"
NCEI_NORMALS = "https://www.ncei.noaa.gov/data/normals-monthly/1991-2020/access/{sid}.csv"
GHCND_STATIONS = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt"
GAZETTEER = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
             "{y}_Gazetteer/{y}_Gaz_place_national.zip")

# City open-data presets (Socrata). Add more as you find them.
PRESETS = {
    "chicago-il": {
        "domain": "data.cityofchicago.org",
        "dataset": "uupf-x98q",          # Business Licenses - Current Active
        "field": "license_description",
        "patterns": ["Mobile Food%", "Special Event Food%"],
        "label": "chicago_business_licenses",
    },
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http_get(url: str, retries: int = 2, timeout: int = TIMEOUT) -> bytes:
    """GET a URL and return the body. Retries on network errors and 5xx.
    Raises HttpError (server answered with an error) or NetworkError (unreachable or timed out)."""
    last: Any = None
    code: Optional[int] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read()[:300].decode("utf-8", "replace")
            except Exception:
                pass
            last, code = f"HTTP {e.code} {body}".strip(), e.code
            if e.code in (400, 401, 403, 404):  # not worth retrying
                break
        except Exception as e:  # network error, timeout, DNS
            last, code = repr(e), None
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    msg = f"GET {url} failed: {last}"
    raise (HttpError(code, msg) if code else NetworkError(msg))


def probe(url: str) -> Tuple[bool, str]:
    """Cheap reachability check: HEAD, then a 1 KB GET if HEAD is refused."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, f"HTTP {resp.status}"
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read(1024)
            return True, f"HTTP {resp.status} (GET)"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, repr(e)[:120]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def slugify(city: str, st: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", f"{city}-{st}".lower()).strip("-")


def parse_city_arg(text: str) -> Tuple[str, str]:
    """'Chicago, IL' | 'Chicago IL' | 'Chicago, Illinois' -> ('Chicago', 'IL')"""
    t = text.strip()
    if "," in t:
        city, st = [p.strip() for p in t.rsplit(",", 1)]
    else:
        parts = t.split()
        if len(parts) < 2:
            raise ValueError(f"Could not find a state in {text!r}. Use 'City, ST'.")
        city, st = " ".join(parts[:-1]), parts[-1]
    if len(st) == 2 and st.upper() in STATES:
        return city, st.upper()
    if st.lower() in ABBR_BY_NAME:
        return city, ABBR_BY_NAME[st.lower()]
    raise ValueError(f"Unknown state {st!r} in {text!r}.")


def num(x: Any) -> Optional[float]:
    """Census numbers -> float, with Census 'missing' codes (negative) -> None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return v


_GOV_WORDS = re.compile(
    r"\b(metropolitan government|metro government|consolidated government|unified government|"
    r"urban county|city and county|balance|municipality)\b", re.I)


def canon(name: str) -> str:
    """Comparable form of a place name: 'Nashville-Davidson metropolitan government (balance)' ->
    'nashvilledavidson'; 'St. Paul' and 'Saint Paul' both -> 'stpaul'."""
    t = re.sub(r"\(.*?\)", " ", name or "")
    t = _GOV_WORDS.sub(" ", t)
    t = re.sub(r"\b(city|village|town|CDP|borough|township)\b\s*$", "", t.strip(), flags=re.I)
    t = re.sub(r"\bsaint\b|\bst\.?(?=\s)", "st", t, flags=re.I)
    t = re.sub(r"\bmount\b|\bmt\.?(?=\s)", "mt", t, flags=re.I)
    t = re.sub(r"\bfort\b|\bft\.?(?=\s)", "ft", t, flags=re.I)
    return norm(t)


def clean_place_name(full: str) -> str:
    """'Chicago city, Illinois' -> 'Chicago'; 'Oak Park village, Illinois' -> 'Oak Park'."""
    name = full.rsplit(",", 1)[0].strip()
    name = re.sub(r"\s*\(balance\)", "", name, flags=re.I).strip()
    m = re.match(r"^(.*)\s+(city|village|town|CDP|borough|municipality|township|urbana)$", name, re.I)
    return m.group(1).strip() if m else name


def write_csv(path: str, rows: List[Dict[str, Any]], fields: Optional[List[str]] = None) -> int:
    if not rows:
        return 0
    if fields is None:
        fields = []
        for r in rows:
            for k in r:
                if k not in fields:
                    fields.append(k)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
    return len(rows)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------
# Census ACS
# --------------------------------------------------------------------------
def census_query(year: int, dataset: str, get: List[str], for_: str,
                 in_: Optional[str], key: Optional[str]) -> List[Dict[str, str]]:
    params = ["get=" + ",".join(get), "for=" + urllib.parse.quote(for_, safe=":*/(),")]
    if in_:
        params.append("in=" + urllib.parse.quote(in_, safe=":*/(),"))
    if key:
        params.append("key=" + key)
    url = f"{CENSUS}/{year}/{dataset}?" + "&".join(params)
    if not key:
        raise CensusKeyError(KEY_HELP)
    raw = http_get(url)
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        # Census answers a bad or missing key with an HTML page, not JSON.
        if b"key" in raw[:2000].lower():
            raise CensusKeyError(KEY_HELP)
        raise RuntimeError(f"Census returned non-JSON for {year}/{dataset}: {raw[:120]!r}")
    header, rows = data[0], data[1:]
    return [dict(zip(header, r)) for r in rows]


def acs_row(raw: Dict[str, str]) -> Dict[str, Any]:
    pop = num(raw.get("B01003_001E"))
    age_total = num(raw.get("B01001_001E"))
    cells = [num(raw.get(v)) for v in AGE_CELLS]
    pct_18_34 = None
    if age_total and all(c is not None for c in cells):
        pct_18_34 = round(sum(c for c in cells if c is not None) / age_total * 100, 1)
    return {
        "population": int(pop) if pop is not None else None,
        "median_hh_income": int(num(raw.get("B19013_001E"))) if num(raw.get("B19013_001E")) is not None else None,
        "median_age": num(raw.get("B01002_001E")),
        "pct_age_18_34": pct_18_34,
        "population_moe": int(num(raw.get("B01003_001M"))) if num(raw.get("B01003_001M")) is not None else None,
        "median_hh_income_moe": (int(num(raw.get("B19013_001M")))
                                 if num(raw.get("B19013_001M")) is not None else None),
    }


def add_asian(row: Dict[str, Any], raw: Optional[Dict[str, str]]) -> None:
    row["asian_total_any_combo"] = None
    row["asian_indian_any_combo"] = None
    row["asian_indian_pct_of_pop"] = None
    if not raw:
        return
    tot, ind = num(raw.get("B02018_001E")), num(raw.get("B02018_002E"))
    row["asian_total_any_combo"] = int(tot) if tot is not None else None
    row["asian_indian_any_combo"] = int(ind) if ind is not None else None
    if ind is not None and row.get("population"):
        row["asian_indian_pct_of_pop"] = round(ind / row["population"] * 100, 2)


def find_acs_year(state_fips: str, key: Optional[str], today: dt.date) -> int:
    last: Any = None
    for y in range(today.year - 1, today.year - 6, -1):
        try:
            rows = census_query(y, "acs/acs5", ["NAME"], "state:" + state_fips, None, key)
            if rows:
                return y
        except CensusKeyError:
            raise
        except Exception as e:
            last = e
    raise RuntimeError(f"No ACS 5-year year worked (last error: {last})")


def load_gazetteer(today: dt.date) -> Tuple[Dict[str, Dict[str, Any]], int]:
    last: Any = None
    for y in range(today.year, today.year - 5, -1):
        try:
            blob = http_get(GAZETTEER.format(y=y))
            zf = zipfile.ZipFile(io.BytesIO(blob))
            name = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
            text = zf.read(name).decode("utf-8-sig", errors="replace")
            parsed = parse_gazetteer(text)
            if not parsed:
                raise RuntimeError(f"Gazetteer {y} parsed to zero rows (format change?)")
            return parsed, y
        except Exception as e:
            last = e
    raise RuntimeError(f"Gazetteer not available (last error: {last})")


def parse_gazetteer(text: str) -> Dict[str, Dict[str, Any]]:
    first = text.split("\n", 1)[0]
    delim = "|" if first.count("|") >= first.count("\t") else "\t"   # the 2026 file is pipe-delimited
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    header = [h.strip() for h in next(reader)]
    ix = {h: i for i, h in enumerate(header)}
    out: Dict[str, Dict[str, Any]] = {}
    for r in reader:
        if len(r) < len(header):
            continue
        try:
            out[r[ix["GEOID"]].strip()] = {
                "land_sqmi": float(r[ix["ALAND_SQMI"]]),
                "lat": float(r[ix["INTPTLAT"]]),
                "lon": float(r[ix["INTPTLONG"]]),
                "name": r[ix["NAME"]].strip() if "NAME" in ix else "",
                "usps": r[ix["USPS"]].strip() if "USPS" in ix else "",
            }
        except (KeyError, ValueError):
            continue
    return out


def find_gaz_place(gaz: Dict[str, Dict[str, Any]], city: str, st: str) -> Optional[str]:
    """Keyless principal-city lookup in the Gazetteer: exact canonical name first, then prefix.
    Prefers real cities over CDPs/townships, then the largest land area."""
    want = canon(city)
    here = {g: v for g, v in gaz.items() if v.get("usps") == st}
    for hits in ([g for g, v in here.items() if canon(v["name"]) == want],
                 [g for g, v in here.items() if canon(v["name"]).startswith(want)]):
        if hits:
            def rank(g: str) -> Tuple[int, float]:
                nm = here[g]["name"].lower()
                return (0 if nm.endswith(" city") or "government" in nm else 1, -here[g]["land_sqmi"])
            return sorted(hits, key=rank)[0]
    return None


def pop_change_note(change_pct: Optional[float], earlier_acs: Optional[float], census2020: Optional[float]) -> str:
    """Say whether a large ACS-to-ACS population change can be trusted, using the 2020 census as a check."""
    if change_pct is None or abs(change_pct) <= 5:
        return ""
    if census2020 and earlier_acs:
        gap = (earlier_acs - census2020) / census2020 * 100
        if abs(gap) > 5:
            return (f"CHECK: the earlier ACS base is {gap:+.0f}% off the 2020 census count, so this change is "
                    "partly an estimate revision: use change_vs_2020_pct instead")
        return ("Large change, and the earlier ACS base agrees with the 2020 census, so it is probably real "
                "growth or decline")
    return "CHECK: over 5% and no 2020 census count to check against; may be an estimate revision or boundary change"


def pull_places(year: int, st: str, key: Optional[str],
                gaz: Optional[Dict[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    fips = STATES[st][0]
    core = census_query(year, "acs/acs5", ["NAME"] + CORE_VARS, "place:*", "state:" + fips, key)
    asian: Dict[str, Dict[str, str]] = {}
    try:
        for r in census_query(year, "acs/acs5", ["NAME"] + ASIAN_VARS, "place:*", "state:" + fips, key):
            asian[r["state"] + r["place"]] = r
    except Exception as e:
        print(f"  ! Asian Indian table failed for {st}: {e}", file=sys.stderr)
    prev: Dict[str, float] = {}
    prev_year = year - 5
    try:
        for r in census_query(prev_year, "acs/acs5", ["NAME", "B01003_001E"], "place:*", "state:" + fips, key):
            v = num(r.get("B01003_001E"))
            if v is not None:
                prev[r["state"] + r["place"]] = v
    except Exception as e:
        print(f"  ! Earlier ACS year ({prev_year}) failed for {st}: {e}", file=sys.stderr)
        prev_year = None  # type: ignore
    census2020: Dict[str, float] = {}
    try:   # 2020 decennial count: a firm reference point for judging the ACS change
        for r in census_query(2020, "dec/pl", ["NAME", "P1_001N"], "place:*", "state:" + fips, key):
            v = num(r.get("P1_001N"))
            if v is not None:
                census2020[r["state"] + r["place"]] = v
    except CensusKeyError:
        raise
    except Exception as e:
        print(f"  ! 2020 census counts failed for {st}: {e}", file=sys.stderr)
    rows: List[Dict[str, Any]] = []
    for r in core:
        geoid = r["state"] + r["place"]
        row: Dict[str, Any] = {"geoid": geoid, "name": clean_place_name(r["NAME"]), "state": st}
        row.update(acs_row(r))
        g = (gaz or {}).get(geoid)
        row["land_sqmi"] = round(g["land_sqmi"], 2) if g else None
        row["lat"] = g["lat"] if g else None
        row["lon"] = g["lon"] if g else None
        row["density_per_sqmi"] = (round(row["population"] / g["land_sqmi"], 1)
                                   if g and g["land_sqmi"] and row.get("population") else None)
        add_asian(row, asian.get(geoid))
        p = prev.get(geoid)
        row["pop_earlier"] = int(p) if p is not None else None
        row["earlier_year"] = prev_year if p is not None else None
        row["pop_change_pct"] = (round((row["population"] - p) / p * 100, 1)
                                 if p and row.get("population") else None)
        c20 = census2020.get(geoid)
        row["census2020_pop"] = int(c20) if c20 is not None else None
        row["change_vs_2020_pct"] = (round((row["population"] - c20) / c20 * 100, 1)
                                     if c20 and row.get("population") else None)
        row["pop_change_note"] = pop_change_note(row["pop_change_pct"], p, c20)
        row["acs_year"] = year
        rows.append(row)
    return rows


def pull_counties(year: int, st: str, key: Optional[str]) -> List[Dict[str, Any]]:
    fips = STATES[st][0]
    core = census_query(year, "acs/acs5", ["NAME"] + CORE_VARS, "county:*", "state:" + fips, key)
    asian: Dict[str, Dict[str, str]] = {}
    try:
        for r in census_query(year, "acs/acs5", ["NAME"] + ASIAN_VARS, "county:*", "state:" + fips, key):
            asian[r["state"] + r["county"]] = r
    except Exception as e:
        print(f"  ! Asian Indian county table failed for {st}: {e}", file=sys.stderr)
    rows = []
    for r in core:
        geoid = r["state"] + r["county"]
        row: Dict[str, Any] = {"geoid": geoid, "name": r["NAME"].rsplit(",", 1)[0].strip(), "state": st}
        row.update(acs_row(r))
        add_asian(row, asian.get(geoid))
        row["acs_year"] = year
        rows.append(row)
    return rows


def pull_metro(year: int, city: str, key: Optional[str], state: Optional[str] = None) -> List[Dict[str, Any]]:
    geo = "metropolitan statistical area/micropolitan statistical area"
    core = census_query(year, "acs/acs5", ["NAME"] + CORE_VARS, geo + ":*", None, key)
    asian: Dict[str, Dict[str, str]] = {}
    try:
        for r in census_query(year, "acs/acs5", ["NAME"] + ASIAN_VARS, geo + ":*", None, key):
            asian[r[geo]] = r
    except Exception as e:
        print(f"  ! Asian Indian metro table failed: {e}", file=sys.stderr)
    want = canon(city)
    rows = []
    for r in core:
        if state and state not in metro_states(r["NAME"]):
            continue
        title = r["NAME"].split(",")[0]
        parts = [canon(t) for t in re.split(r"[-/]", title)]
        if want != canon(title) and want not in parts:
            continue
        row: Dict[str, Any] = {"cbsa": r[geo], "name": r["NAME"]}
        row.update(acs_row(r))
        add_asian(row, asian.get(r[geo]))
        row["acs_year"] = year
        rows.append(row)
    rows.sort(key=lambda x: -(x.get("population") or 0))
    return rows


def metro_states(cbsa_name: str) -> List[str]:
    m = re.search(r",\s*([A-Z]{2}(?:-[A-Z]{2})*)\s+(?:Metro|Micro)", cbsa_name)
    return [s for s in m.group(1).split("-") if s in STATES] if m else []


def pick_core(rows: List[Dict[str, Any]], principal: Optional[Dict[str, Any]],
              wanted: List[str], top_n: int,
              radius_miles: Optional[float] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    chosen: List[Dict[str, Any]] = []
    seen = set()
    if principal:
        chosen.append(principal)
        seen.add(principal["geoid"])
    missing: List[str] = []
    if wanted:
        for w in wanted:
            hits = [r for r in rows if canon(r["name"]) == canon(w)]
            if not hits:
                hits = [r for r in rows if canon(r["name"]).startswith(canon(w))]
            if not hits:
                missing.append(w)
                continue
            best = max(hits, key=lambda r: r.get("population") or 0)
            if best["geoid"] not in seen:
                chosen.append(best)
                seen.add(best["geoid"])
    else:
        # No --places: the largest places near the principal city (not the largest in the state:
        # that would list Memphis as a Nashville suburb).
        near = rows
        if principal and radius_miles and principal.get("lat") is not None:
            near = [r for r in rows if r.get("lat") is not None and
                    haversine_miles(principal["lat"], principal["lon"], r["lat"], r["lon"]) <= radius_miles]
        for r in sorted(near, key=lambda r: -(r.get("population") or 0)):
            if len(chosen) >= top_n:
                break
            if r["geoid"] not in seen:
                chosen.append(r)
                seen.add(r["geoid"])
    return chosen, missing


# --------------------------------------------------------------------------
# NOAA climate normals
# --------------------------------------------------------------------------
def parse_ghcnd_stations(text: str) -> List[Dict[str, Any]]:
    out = []
    for line in text.splitlines():
        if len(line) < 45:
            continue
        try:
            out.append({
                "id": line[0:11].strip(),
                "lat": float(line[12:20]),
                "lon": float(line[21:30]),
                "state": line[38:40].strip(),
                "name": line[41:71].strip(),
            })
        except ValueError:
            continue
    return out


def nearest_airport_stations(stations: List[Dict[str, Any]], lat: float, lon: float,
                             max_miles: float = 75.0, limit: int = 8) -> List[Dict[str, Any]]:
    cands = []
    for s in stations:
        if not s["id"].startswith("USW"):         # USW = first-order weather stations (mostly airports)
            continue
        if re.search(r"\b\d+\s+[NSEW]{1,3}\b", s["name"]):    # "LIMESTONE 4 NNW": offset observer site
            continue
        d = haversine_miles(lat, lon, s["lat"], s["lon"])
        if d <= max_miles:
            cands.append(dict(s, miles=round(d, 1)))
    cands.sort(key=lambda s: s["miles"])
    return cands[:limit]


STATION_WINDOW_MILES = 12.0   # only consider stations this much farther than the nearest airport

NORMALS_MAP = [
    ("avg_high_f", r"^MLY-TMAX-NORMAL$"),
    ("avg_low_f", r"^MLY-TMIN-NORMAL$"),
    ("avg_mean_f", r"^MLY-TAVG-NORMAL$"),
    ("precip_in", r"^MLY-PRCP-NORMAL$"),
    ("snow_in", r"^MLY-SNOW-NORMAL$"),
    ("days_high_ge_50f", r"^MLY-TMAX-AVGNDS-GRTH050$"),
    ("days_high_ge_70f", r"^MLY-TMAX-AVGNDS-GRTH070$"),
    ("days_high_ge_90f", r"^MLY-TMAX-AVGNDS-GRTH090$"),
    ("days_high_le_32f", r"^MLY-TMAX-AVGNDS-LSTH032$"),
    ("days_low_le_32f", r"^MLY-TMIN-AVGNDS-LSTH032$"),
    ("days_precip_ge_0.01in", r"^MLY-PRCP-AVGNDS-GE001"),
    ("days_precip_ge_0.10in", r"^MLY-PRCP-AVGNDS-GE010"),
]


AIRPORT_NAME = re.compile(r"\b(AP|ARPT|AIRPORT|INTL|INTERNATIONAL|JETPORT|RGNL|REGIONAL|FLD|FIELD|NAS|AFB|AAF)\b")


def choose_station(cands: List[Dict[str, Any]], fetch) -> Optional[Dict[str, Any]]:
    """Pick ONE station: among those no more than STATION_WINDOW_MILES farther than the nearest, the one with
    the most complete monthly normals; ties go to an airport station, then to the nearest. (Non-airport sites
    near a city can hold the nearest slots, so the whole window is considered, up to 6 stations.)"""
    if not cands:
        return None
    nearest = cands[0]["miles"]
    window = [c for c in cands if c["miles"] <= nearest + STATION_WINDOW_MILES][:6]
    best: Optional[Dict[str, Any]] = None
    for i, c in enumerate(window):
        try:
            text = fetch(c["id"])
        except Exception:
            continue
        key = (normals_completeness(tidy_normals(text)), bool(AIRPORT_NAME.search(c["name"])), -i)
        if best is None or key > best["key"]:
            best = {"key": key, "id": c["id"], "text": text,
                    "label": f"{c['name']} ({c['id']}, {c['miles']} mi from city center)"}
    return best


def normals_completeness(rows: List[Dict[str, Any]]) -> int:
    """How many of the key monthly fields are filled (higher = better station choice)."""
    keys = ("avg_high_f", "avg_low_f", "precip_in", "snow_in", "days_high_ge_90f", "days_low_le_32f")
    return sum(1 for r in rows for k in keys if r.get(k) is not None)


def tidy_normals(text: str) -> List[Dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    picks: Dict[str, Optional[str]] = {}
    for out_name, pat in NORMALS_MAP:
        picks[out_name] = next((c for c in cols if re.search(pat, c)), None)
    rows = []
    for r in reader:
        try:
            month = int(str(r.get("DATE", "")).strip()[-2:])
        except ValueError:
            continue
        row: Dict[str, Any] = {"month": month}
        for out_name, col in picks.items():
            val = None
            if col:
                try:
                    v = float(r[col])
                    val = None if v in (-9999.0, -7777.0) else v
                except (TypeError, ValueError):
                    val = None
            row[out_name] = val
        rows.append(row)
    rows.sort(key=lambda x: x["month"])
    return rows


# --------------------------------------------------------------------------
# Urban Institute Education Data API (NCES CCD districts, IPEDS colleges)
# --------------------------------------------------------------------------
URBAN_TIMEOUT = 45   # the service is slow when it is busy; a timeout means it is down, so do not wait long


def urban_fetch_all(path: str, params: Dict[str, Any], cap: int = 60000) -> List[Dict[str, Any]]:
    url: Optional[str] = f"{URBAN}/{path}?" + urllib.parse.urlencode(params)
    out: List[Dict[str, Any]] = []
    while url:
        d = json.loads(http_get(url, retries=1, timeout=URBAN_TIMEOUT))
        out.extend(d.get("results", []))
        url = d.get("next")
        if len(out) >= cap:
            break
    return out


def urban_latest(path_tpl: str, st_fips: str, today: dt.date) -> Tuple[List[Dict[str, Any]], int]:
    last: Any = None
    for y in range(today.year - 2, today.year - 7, -1):
        try:
            rows = urban_fetch_all(path_tpl.format(year=y), {"fips": st_fips})
            if rows:
                return rows, y
        except NetworkError:
            raise                      # unreachable or timed out: another year will not help
        except Exception as e:         # this year is not published (HTTP 404/400) or bad JSON
            last = e
    raise RuntimeError(f"No year worked for {path_tpl} (last error: {last})")


# --------------------------------------------------------------------------
# Run stamp: never mix sections written by an older run or an older version of the skill
# --------------------------------------------------------------------------
SKILL_FILES = ("SKILL.md", "modules.md", "report-format.md")
RESUME_MAX_DAYS = 3


def skill_version(skill_dir: Optional[str] = None) -> str:
    """Short hash of the three instruction files: changes whenever the skill's rules change."""
    import hashlib
    d = skill_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    h = hashlib.sha256()
    for name in SKILL_FILES:
        try:
            with open(os.path.join(d, name), "rb") as f:
                h.update(name.encode() + f.read())
        except OSError:
            h.update(name.encode() + b"missing")
    return h.hexdigest()[:12]


def start_run(outdir: str, city: str, today: dt.date, version: str,
              max_age_days: int = RESUME_MAX_DAYS) -> Dict[str, Any]:
    """Write reports/<slug>/run.json. If sections/ from an earlier run exist and the run is older than
    `max_age_days` or was made with a different skill version, move them to sections_archive/<stamp>/
    (with report.md/report.html) so the new run redoes them instead of silently reusing stale work."""
    import shutil
    path = os.path.join(outdir, "run.json")
    prev: Dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}
    sections = os.path.join(outdir, "sections")
    has_sections = os.path.isdir(sections) and any(n.endswith(".md") for n in os.listdir(sections))
    reason = ""
    started = today.isoformat()
    if has_sections:
        try:
            age = (today - dt.date.fromisoformat(prev.get("started", ""))).days
        except ValueError:
            age = None
        if not prev:
            reason = "no run.json (sections come from an unknown earlier run)"
        elif prev.get("skill_version") != version:
            reason = f"the skill changed since that run ({prev.get('skill_version')} -> {version})"
        elif age is None or age > max_age_days:
            reason = f"that run is {age if age is not None else '?'} days old (limit {max_age_days})"
    archived = ""
    if reason:
        stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
        archived = os.path.join(outdir, "sections_archive", stamp)
        os.makedirs(archived, exist_ok=True)
        shutil.move(sections, os.path.join(archived, "sections"))
        for name in ("report.md", "report.html"):
            if os.path.exists(os.path.join(outdir, name)):
                shutil.move(os.path.join(outdir, name), os.path.join(archived, name))
    elif has_sections and prev.get("started"):
        started = prev["started"]               # resuming the same run: keep its start date
    info = {"city": city, "started": started, "skill_version": version, "pulled": today.isoformat()}
    os.makedirs(outdir, exist_ok=True)
    manual = os.path.join(outdir, "manual")
    os.makedirs(manual, exist_ok=True)
    note = os.path.join(manual, "README.txt")
    if not os.path.exists(note):
        with open(note, "w", encoding="utf-8") as f:
            f.write("Pages the automated tools could not open (the report's Open questions lists them).\n"
                    "Open each page in your browser and save it here (File > Save Page As, or Print > Save as PDF),\n"
                    "then run the report again: everything in this folder is read and quoted.\n")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    return {"info": info, "archived": archived, "reason": reason, "resumed": bool(has_sections and not reason)}


def blank_missing_codes(rows: List[Dict[str, Any]]) -> None:
    """Urban Institute uses -1, -2, -3 for missing/not applicable."""
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, (int, float)) and v in (-1, -2, -3) and k not in ("fips",):
                r[k] = None


# --------------------------------------------------------------------------
# OpenStreetMap place-of-interest counts (Overpass API)
# --------------------------------------------------------------------------
OVERPASS = "https://overpass-api.de/api/interpreter"
POI_SETS: List[Tuple[str, str]] = [
    ("breweries", 'nwr["craft"="brewery"]{a};nwr["microbrewery"="yes"]{a};'),
    ("bars_pubs", 'nwr["amenity"~"^(bar|pub)$"]{a};'),
    ("restaurants", 'nwr["amenity"="restaurant"]{a};'),
    ("indian_restaurants", 'nwr["amenity"="restaurant"]["cuisine"~"indian|pakistani|bangladeshi|nepalese|'
                           'sri_lankan|tandoori"]{a};'),
    ("vegetarian_vegan_restaurants", 'nwr["amenity"="restaurant"]["diet:vegetarian"="only"]{a};'
                                     'nwr["amenity"="restaurant"]["diet:vegan"="only"]{a};'
                                     'nwr["amenity"="restaurant"]["cuisine"~"vegetarian|vegan"]{a};'),
    ("colleges_universities", 'nwr["amenity"~"^(university|college)$"]{a};'),
    ("hindu_temples", 'nwr["amenity"="place_of_worship"]["religion"="hindu"]{a};'),
]


POI_RADIUS_M = 5000   # the same circle for every place, so the counts can be compared between places


def place_search_name(name: str) -> str:
    """'Nashville-Davidson metropolitan government' -> 'Nashville'; 'Louisville/Jefferson County ...' -> 'Louisville'."""
    t = re.split(r"[-/(]", name)[0]
    t = _GOV_WORDS.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def center_query(lat: float, lon: float, name: str, radius_m: int = 20000) -> str:
    n = re.sub(r"[^\w \-]", ".", place_search_name(name))   # punctuation becomes a regex wildcard: no escapes
    return (f'[out:json][timeout:30];node["place"~"^(city|town|village)$"]["name"~"^{n}$",i]'
            f"(around:{radius_m},{lat:.5f},{lon:.5f});out;")


def parse_center(text: str, lat: float, lon: float) -> Optional[Tuple[float, float]]:
    """Pick the OSM place node: biggest kind first (city > town > village), then nearest to the Census point."""
    rank = {"city": 0, "town": 1, "village": 2}
    nodes = [e for e in json.loads(text).get("elements", []) if e.get("type") == "node" and "lat" in e]
    if not nodes:
        return None
    best = min(nodes, key=lambda e: (rank.get(e.get("tags", {}).get("place"), 3),
                                     haversine_miles(lat, lon, e["lat"], e["lon"])))
    return best["lat"], best["lon"]


def poi_query(lat: float, lon: float, radius_m: int) -> str:
    around = f"(around:{radius_m},{lat:.5f},{lon:.5f})"
    body = "".join(f"({expr.format(a=around)});out count;" for _, expr in POI_SETS)
    return f"[out:json][timeout:60];{body}"


def parse_poi(text: str) -> Dict[str, Optional[int]]:
    els = [e for e in json.loads(text).get("elements", []) if e.get("type") == "count"]
    if len(els) != len(POI_SETS):
        raise RuntimeError(f"Overpass returned {len(els)} counts, expected {len(POI_SETS)}")
    return {name: int(e["tags"]["total"]) for (name, _), e in zip(POI_SETS, els)}


OVERPASS_MIRRORS = [OVERPASS, "https://overpass.kumi.systems/api/interpreter",
                    "https://overpass.private.coffee/api/interpreter"]
POI_FIELDS = ["name", "state", "radius_m", "center"] + [n for n, _ in POI_SETS]


def overpass_counts(lat: float, lon: float, radius_m: int,
                    waits: Tuple[float, ...] = (0, 10, 30, 60, 90)) -> Dict[str, Optional[int]]:
    """One place's counts. Overpass answers 429/502/504 when busy: back off exponentially and
    alternate between the public mirrors."""
    q = urllib.parse.quote(poi_query(lat, lon, radius_m))
    last: Any = None
    for i, wait in enumerate(waits):
        if wait:
            time.sleep(wait)
        url = OVERPASS_MIRRORS[i % len(OVERPASS_MIRRORS)] + "?data=" + q
        try:
            return parse_poi(http_get(url, retries=0, timeout=100).decode("utf-8", "replace"))
        except (HttpError, NetworkError, ValueError, RuntimeError) as e:
            last = e
    raise RuntimeError(f"Overpass busy or unreachable after {len(waits)} tries: {str(last)[:160]}")


def find_center(pl: Dict[str, Any], waits: Tuple[float, ...]) -> Tuple[float, float, str]:
    """Center for the counts: the OpenStreetMap place node for the town if found, else the Census
    internal point (which can fall in water or a park for coastal or irregular places)."""
    q = urllib.parse.quote(center_query(pl["lat"], pl["lon"], pl["name"]))
    for i, wait in enumerate(waits[:3]):
        if wait:
            time.sleep(wait)
        url = OVERPASS_MIRRORS[i % len(OVERPASS_MIRRORS)] + "?data=" + q
        try:
            c = parse_center(http_get(url, retries=0, timeout=60).decode("utf-8", "replace"), pl["lat"], pl["lon"])
            if c:
                return c[0], c[1], "osm place node"
            break
        except (HttpError, NetworkError, ValueError, RuntimeError):
            continue
    return pl["lat"], pl["lon"], "census internal point"


def pull_poi(places: List[Dict[str, Any]], path: str, pause: float = 1.0, max_places: int = 13,
             waits: Tuple[float, ...] = (0, 10, 30, 60, 90), radius_m: int = POI_RADIUS_M,
             workers: int = 2, max_failures: int = 3, recenter: bool = True,
             retry_failed: bool = True) -> Tuple[int, List[str]]:
    """Write poi_counts_osm.csv, one row per finished place (partial results survive an interruption).
    Two requests at a time (what the public Overpass service allows); stops after `max_failures` failures.
    Returns (rows written, error messages)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    todo = [pl for pl in places[:max_places] if pl.get("lat") is not None]
    errors: List[str] = []
    failed: List[Dict[str, Any]] = []
    stopped = False
    n = 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    partial = path + ".partial"      # the real name appears only when the step is finished

    def work(pl: Dict[str, Any]):
        clat, clon, how = find_center(pl, waits) if recenter else (pl["lat"], pl["lon"], "given point")
        counts = overpass_counts(clat, clon, radius_m, waits)
        time.sleep(pause)
        return pl, how, counts

    with open(partial, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=POI_FIELDS)
        w.writeheader()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(work, pl): pl for pl in todo}
            for fut in as_completed(futs):
                pl = futs[fut]
                try:
                    _, how, counts = fut.result()
                except RuntimeError as e:
                    errors.append(f"{pl['name']}: {e}")
                    if len(errors) >= max_failures:
                        errors.append(f"stopped after {max_failures} failures")
                        for other in futs:
                            other.cancel()
                        stopped = True
                        break
                    failed.append(pl)
                    continue
                w.writerow(dict({"name": pl["name"], "state": pl["state"], "radius_m": radius_m, "center": how},
                                **counts))
                f.flush()
                n += 1
        if retry_failed and failed and not stopped:      # one more go at the places that failed, one at a time,
            for pl in failed:                            # when the busy service may have calmed down
                try:
                    clat, clon, how = find_center(pl, waits) if recenter else (pl["lat"], pl["lon"], "given point")
                    counts = overpass_counts(clat, clon, radius_m, (30, 60, 90))
                except RuntimeError as e:
                    errors.append(f"{pl['name']}: still failing on retry: {e}")
                    continue
                w.writerow(dict({"name": pl["name"], "state": pl["state"], "radius_m": radius_m, "center": how},
                                **counts))
                f.flush()
                n += 1
                errors = [x for x in errors if not x.startswith(pl["name"] + ":")]
    os.replace(partial, path)
    return n, errors


NEIGHBORHOOD_RADIUS_M = 1500
PLACE_RANK = {"suburb": 0, "quarter": 1, "neighbourhood": 2}


def neighborhood_query(lat: float, lon: float, radius_m: int, names: Optional[List[str]] = None) -> str:
    around = f"(around:{radius_m},{lat:.5f},{lon:.5f})"
    if names:
        rx = "|".join(re.sub(r"[^\w \-]", ".", n.strip()) for n in names if n.strip())
        return f'[out:json][timeout:60];node["place"~"^(suburb|quarter|neighbourhood)$"]["name"~"^({rx})$",i]{around};out;'
    return f'[out:json][timeout:60];node["place"~"^(suburb|quarter|neighbourhood)$"]["name"]{around};out;'


def parse_neighborhoods(text: str, cap: int = 25, center: Optional[Tuple[float, float]] = None) -> List[Dict[str, Any]]:
    """Named OSM neighborhoods (place=suburb/quarter/neighbourhood), one per name, capped.
    With `center` (lat, lon) the closest to the city center come first, so a cap keeps the core districts;
    without it, bigger place kinds first and then by name (the old order, which cut the list alphabetically)."""
    seen: Dict[str, Dict[str, Any]] = {}
    for e in json.loads(text).get("elements", []):
        tags = e.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if e.get("type") != "node" or not name or "lat" not in e:
            continue
        rec = {"name": name, "state": "", "place": tags.get("place", ""), "lat": e["lat"], "lon": e["lon"]}
        old = seen.get(name.lower())
        if old is None or PLACE_RANK.get(rec["place"], 3) < PLACE_RANK.get(old["place"], 3):
            seen[name.lower()] = rec
    if center is not None:
        def dist(r: Dict[str, Any]) -> float:
            return math.hypot((r["lat"] - center[0]) * 111.0, (r["lon"] - center[1]) * 111.0 * math.cos(math.radians(center[0])))
        ranked = sorted(seen.values(), key=lambda r: (dist(r), PLACE_RANK.get(r["place"], 3), r["name"]))
    else:
        ranked = sorted(seen.values(), key=lambda r: (PLACE_RANK.get(r["place"], 3), r["name"]))
    return ranked[:cap]


def neighborhoods_only(outdir: str, names: Optional[List[str]] = None, cap: int = 25) -> int:
    """`--neighborhoods-only`: counts of bars, restaurants, breweries, colleges around the principal city's
    neighborhoods (OpenStreetMap), written to neighborhood_counts_osm.csv. Slow: run in the background."""
    data = os.path.join(outdir, "data")
    core = os.path.join(data, "acs_places_core.csv")
    if not os.path.exists(core):
        print(f"Need {core} first: run the normal pull (with a Census key) before --neighborhoods-only", file=sys.stderr)
        return 2
    with open(core, encoding="utf-8", newline="") as f:
        first = next(csv.DictReader(f))
    try:
        lat, lon, sq = float(first["lat"]), float(first["lon"]), float(first.get("land_sqmi") or 50)
    except (KeyError, ValueError):
        print("acs_places_core.csv has no lat/lon for the principal city", file=sys.stderr)
        return 2
    search_m = int(min(30000, max(4000, math.sqrt(sq / math.pi) * 1609.34 * 1.3)))
    print(f"Looking for neighborhoods within {search_m / 1000:.0f} km of {first['name']} ...", flush=True)
    try:
        url = OVERPASS_MIRRORS[0] + "?data=" + urllib.parse.quote(neighborhood_query(lat, lon, search_m, names))
        hoods = parse_neighborhoods(http_get(url, retries=2, timeout=90).decode("utf-8", "replace"), cap, (lat, lon))
    except Exception as e:
        print(f"Could not list neighborhoods: {e}", file=sys.stderr)
        return 1
    if not hoods:
        print("No named neighborhoods found in OpenStreetMap for this city.")
        return 1
    for h in hoods:
        h["state"] = first.get("state", "")
    print(f"Counting around {len(hoods)} neighborhoods, {NEIGHBORHOOD_RADIUS_M / 1000:g} km each "
          "(10 minutes or more when the public service is busy) ...", flush=True)
    n, errs = pull_poi(hoods, os.path.join(data, "neighborhood_counts_osm.csv"), max_places=cap,
                       radius_m=NEIGHBORHOOD_RADIUS_M, recenter=False)
    status = (f"{n} of {len(hoods)} neighborhoods written." if n else "No rows written.") + \
        (" Problems: " + "; ".join(errs) if errs else "")
    with open(os.path.join(data, "MANIFEST.md"), "a", encoding="utf-8") as f:
        f.write("\n## Neighborhood counts (added by --neighborhoods-only)\n"
                f"- neighborhood_counts_osm.csv, {dt.date.today().isoformat()}: {status} OpenStreetMap neighborhoods "
                f"(place=suburb/quarter/neighbourhood), same {NEIGHBORHOOD_RADIUS_M // 1000 or 1.5} km circle for each; "
                "no population signal; volunteer-mapped, so relative proxies only (low confidence).\n")
    print(status)
    return 0 if n else 1


def poi_only(outdir: str, max_places: int = 13) -> int:
    """`--poi-only`: add poi_counts_osm.csv to an existing pull (run it in the background)."""
    data = os.path.join(outdir, "data")
    core_path = os.path.join(data, "acs_places_core.csv")
    if not os.path.exists(core_path):
        print(f"Need {core_path} first: run the normal pull (with a Census key) before --poi-only", file=sys.stderr)
        return 2

    def fl(v: Any) -> Optional[float]:
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None
    with open(core_path, encoding="utf-8", newline="") as f:
        places = [dict(r, lat=fl(r.get("lat")), lon=fl(r.get("lon")), land_sqmi=fl(r.get("land_sqmi")))
                  for r in csv.DictReader(f)]
    print(f"OpenStreetMap counts for up to {max_places} places, {POI_RADIUS_M / 1000:g} km around each center "
          "(can take 10 minutes or more when the public service is busy) ...", flush=True)
    n, errs = pull_poi(places, os.path.join(data, "poi_counts_osm.csv"), max_places=max_places)
    status = (f"{n} places written." if n else "No rows written.") + (" Problems: " + "; ".join(errs) if errs else "")
    with open(os.path.join(data, "MANIFEST.md"), "a", encoding="utf-8") as f:
        f.write("\n## OpenStreetMap counts (added by --poi-only)\n"
                f"- poi_counts_osm.csv, {dt.date.today().isoformat()}: {status} Counts are within radius_m of each "
                f"place's center, same {POI_RADIUS_M // 1000} km circle for every place (so they include neighboring areas "
                "and are comparable between places); OSM is volunteer-mapped and undercounts in some places: "
                "relative proxies only (low confidence).\n")
    print(status)
    return 0 if n else 1


# --------------------------------------------------------------------------
# Socrata open data (city license counts)
# --------------------------------------------------------------------------
def socrata_url(domain: str, dataset: str, params: Dict[str, str]) -> str:
    q = "&".join(f"{k}={urllib.parse.quote(v, safe=',()*')}" for k, v in params.items())
    return f"https://{domain}/resource/{dataset}.json?{q}"


def socrata_counts(domain: str, dataset: str, field: str) -> List[Dict[str, Any]]:
    url = socrata_url(domain, dataset, {
        "$select": f"{field},count(*)", "$group": field,
        "$order": "count DESC", "$limit": "2000"})
    return json.loads(http_get(url))


def socrata_where(field: str, patterns: List[str]) -> str:
    return " OR ".join(f"{field} like '{p}'" for p in patterns)


def socrata_rows(domain: str, dataset: str, field: str, patterns: List[str],
                 page: int = 5000, cap: int = 50000) -> List[Dict[str, Any]]:
    """Rows whose license field matches the patterns (SQL LIKE, e.g. 'Mobile Food%'), paged."""
    out: List[Dict[str, Any]] = []
    where = socrata_where(field, patterns)
    while len(out) < cap:
        url = socrata_url(domain, dataset, {"$where": where, "$order": ":id",
                                            "$limit": str(page), "$offset": str(len(out))})
        chunk = json.loads(http_get(url))
        out.extend(chunk)
        if len(chunk) < page:
            break
    return out


# --------------------------------------------------------------------------
# Main run
# --------------------------------------------------------------------------
class Run:
    def __init__(self) -> None:
        self.log: List[Dict[str, Any]] = []
        self.files: List[Dict[str, Any]] = []
        self.urban_down = False   # set when the Urban Institute API times out: skip its remaining calls

    def attempt(self, name: str, fn):
        t0 = time.time()
        print(f"- {name} ...", flush=True)
        try:
            result = fn()
            self.log.append({"step": name, "ok": True, "seconds": round(time.time() - t0, 1)})
            return result
        except Exception as e:
            msg = str(e)[:600]
            print(f"  FAILED: {msg}", file=sys.stderr)
            self.log.append({"step": name, "ok": False, "error": msg})
            return None

    def file(self, name: str, source: str, year: Any, rows: int, note: str = "") -> None:
        self.files.append({"file": name, "source": source, "year": year, "rows": rows, "note": note})


def run(args: argparse.Namespace) -> int:
    city, st = parse_city_arg(args.city)
    slug = slugify(city, st)
    outdir = args.outdir or os.path.join("reports", slug)
    data = os.path.join(outdir, "data")
    os.makedirs(data, exist_ok=True)
    with open(os.path.join(outdir, "city.txt"), "w", encoding="utf-8") as f:
        f.write(f"{city}, {st}\n")
    key = get_census_key(args.census_key)
    today = dt.date.today()
    stamp = start_run(outdir, f"{city}, {st}", today, skill_version())
    if stamp["archived"]:
        print(f"! Older sections moved to {stamp['archived']} because {stamp['reason']}. "
              "This run redoes every module.", file=sys.stderr)
    elif stamp["resumed"]:
        print(f"Resuming the run started {stamp['info']['started']}: existing sections are kept.")
    R = Run()
    wanted = [p.strip() for p in (args.places or "").split(",") if p.strip()]

    print(f"Pulling data for {city}, {st} into {data}")

    # 1. ACS year + metro
    acs_year = R.attempt("Find latest ACS 5-year year", lambda: find_acs_year(STATES[st][0], key, today))
    if not acs_year and not key:
        print("  ! No Census key: ACS steps skipped. " + KEY_HELP, file=sys.stderr)
    metro_rows: List[Dict[str, Any]] = []
    states = [s.strip().upper() for s in args.states.split(",")] if args.states else []
    if acs_year:
        metro_rows = R.attempt("Metro totals (ACS)", lambda: pull_metro(acs_year, city, key, st)) or []
        if metro_rows:
            n = write_csv(os.path.join(data, "acs_metro.csv"), metro_rows)
            R.file("acs_metro.csv", "Census ACS 5-year (api.census.gov)", acs_year, n,
                   f"Best match: {metro_rows[0]['name']}")
            if not states:
                states = metro_states(metro_rows[0]["name"])
    if not states:
        states = [st]
    if st not in states:
        states.insert(0, st)

    # 2. Gazetteer (land area -> density, and city lat/lon for the weather station)
    gaz_out = R.attempt("Census Gazetteer (land area)", lambda: load_gazetteer(today))
    gaz, gaz_year = (gaz_out if gaz_out else (None, None))
    gaz_principal = find_gaz_place(gaz, city, st) if gaz else None

    # 3. Places and counties for each metro state
    all_places: List[Dict[str, Any]] = []
    if acs_year:
        for s in states:
            rows = R.attempt(f"ACS places in {s}", lambda s=s: pull_places(acs_year, s, key, gaz))
            if rows:
                n = write_csv(os.path.join(data, f"acs_places_all_{s}.csv"), rows)
                R.file(f"acs_places_all_{s}.csv", "Census ACS 5-year + Gazetteer", acs_year, n,
                       "All places in the state (not filtered to the metro)")
                all_places.extend(rows)
            crow = R.attempt(f"ACS counties in {s}", lambda s=s: pull_counties(acs_year, s, key))
            if crow:
                n = write_csv(os.path.join(data, f"acs_counties_{s}.csv"), crow)
                R.file(f"acs_counties_{s}.csv", "Census ACS 5-year", acs_year, n,
                       "All counties in the state (not filtered to the metro)")

    principal = None
    if all_places:
        same_state = [r for r in all_places if r["state"] == st]
        exact = [r for r in same_state if canon(r["name"]) == canon(city)]
        pool = exact or [r for r in same_state if canon(r["name"]).startswith(canon(city))]
        if pool:
            principal = max(pool, key=lambda r: r.get("population") or 0)
        core, missing = pick_core(all_places, principal, wanted, args.top, args.radius)
        n = write_csv(os.path.join(data, "acs_places_core.csv"), core)
        note = ("Principal city + requested places" if wanted else
                f"Principal city + the {args.top} largest places within {args.radius:g} miles of it "
                "(a convenience list: choose the real core suburbs yourself and pass --places)")
        if missing:
            note += f". NOT FOUND by name: {', '.join(missing)}"
        R.file("acs_places_core.csv", "Census ACS 5-year + Gazetteer", acs_year, n, note)
        if not principal:
            R.log.append({"step": "Principal city match", "ok": False,
                          "error": f"No place named {city} found in {st}"})

    # 4. NOAA climate normals
    def noaa() -> str:
        if args.station:
            sid, label = args.station, args.station
        else:
            pgeo = principal["geoid"] if principal else gaz_principal
            if not (gaz and pgeo and pgeo in gaz):
                raise RuntimeError("Need the city's coordinates (Gazetteer + city match) or pass --station")
            g = gaz[pgeo]
            stations = parse_ghcnd_stations(http_get(GHCND_STATIONS).decode("ascii", "replace"))
            cands = nearest_airport_stations(stations, g["lat"], g["lon"])
            if not cands:
                raise RuntimeError("No airport weather station within 75 miles; pass --station")
            best = choose_station(cands, lambda sid: http_get(NCEI_NORMALS.format(sid=sid)).decode("utf-8", "replace"))
            if not best:
                raise RuntimeError("No NOAA normals file found for the nearest stations; pass --station")
            return finish_noaa(best["id"], best["label"], best["text"])
        text = http_get(NCEI_NORMALS.format(sid=sid)).decode("utf-8", "replace")
        return finish_noaa(sid, label, text)

    def finish_noaa(sid: str, label: str, text: str) -> str:
        with open(os.path.join(data, "noaa_normals_raw.csv"), "w", encoding="utf-8") as f:
            f.write(text)
        rows = tidy_normals(text)
        if not rows:
            raise RuntimeError("NOAA file parsed to zero rows")
        n = write_csv(os.path.join(data, "noaa_monthly_normals.csv"), rows)
        R.file("noaa_monthly_normals.csv", "NOAA NCEI 1991-2020 monthly normals", "1991-2020", n,
               f"Station: {label}. Temperatures in F, precipitation and snow in inches. "
               "Blank = not in the NOAA file (see noaa_normals_raw.csv)")
        return label

    R.attempt("NOAA monthly normals", noaa)

    # 5. Schools and colleges (all metro states; filter to the metro yourself)
    def urban_step(name: str, tpl: str, fips: str):
        if R.urban_down:
            R.log.append({"step": name, "ok": False,
                          "error": "skipped: the Urban Institute API was unreachable earlier in this run"})
            return None

        def go():
            try:
                return urban_latest(tpl, fips, today)
            except NetworkError:
                R.urban_down = True
                raise
        return R.attempt(name, go)

    if not args.no_schools:
        for s in states:
            fips = STATES[s][0]
            got = urban_step(f"School districts in {s}", "school-districts/ccd/directory/{year}/", fips)
            if got:
                rows, y = got
                blank_missing_codes(rows)
                n = write_csv(os.path.join(data, f"schools_districts_{s}.csv"), rows)
                R.file(f"schools_districts_{s}.csv", "NCES Common Core of Data via Urban Institute API", y, n,
                       "State-wide. Filter to the metro counties/cities. Blank = missing code")
            got = urban_step(f"Colleges in {s}", "college-university/ipeds/directory/{year}/", fips)
            if got:
                rows, y = got
                blank_missing_codes(rows)
                n = write_csv(os.path.join(data, f"colleges_{s}.csv"), rows)
                R.file(f"colleges_{s}.csv", "IPEDS directory via Urban Institute API", y, n,
                       "State-wide directory (names, city, county, type). Enrollment is NOT included: look it up")

    # 5b. Metro-only school and college files (keyed on the Census CBSA code)
    if not args.no_schools:
        R.attempt("Filter schools and colleges to the metro",
                  lambda: write_metro_files(data, states, city, st, metro_rows, R))

    # 6. City open-data license counts
    soc = None
    if args.socrata_domain and args.socrata_dataset:
        soc = {"domain": args.socrata_domain, "dataset": args.socrata_dataset,
               "field": args.socrata_field or "license_description",
               "patterns": [p.strip() for p in (args.socrata_search or "Mobile Food%").split(",") if p.strip()],
               "label": re.sub(r"[^a-z0-9]+", "_", args.socrata_domain.lower())}
    elif slug in PRESETS:
        soc = PRESETS[slug]
    if soc:
        counts = R.attempt("City open data: license counts by type",
                           lambda: socrata_counts(soc["domain"], soc["dataset"], soc["field"]))
        if counts:
            n = write_csv(os.path.join(data, f"{soc['label']}_license_counts.csv"), counts)
            R.file(f"{soc['label']}_license_counts.csv", f"{soc['domain']} dataset {soc['dataset']}", today.year, n,
                   "Rows per license type in the dataset as of the pull date (not unique businesses)")
        found = R.attempt("City open data: mobile food rows",
                          lambda: socrata_rows(soc["domain"], soc["dataset"], soc["field"], soc["patterns"]))
        if found:
            n = write_csv(os.path.join(data, f"{soc['label']}_mobile_food_rows.csv"), found)
            R.file(f"{soc['label']}_mobile_food_rows.csv", f"{soc['domain']} dataset {soc['dataset']}", today.year, n,
                   f"{soc['field']} like {soc['patterns']}. One row per LICENSE, not per unique truck: "
                   "check status/expiration columns and dedupe by business before counting trucks")

    write_manifest(outdir, data, city, st, today, R, gaz_year, acs_year)
    ok = sum(1 for e in R.log if e["ok"])
    bad = len(R.log) - ok
    print(f"\nDone: {ok} steps ok, {bad} failed. See {os.path.join(data, 'MANIFEST.md')}")
    return 0 if R.files else 1


def metro_cbsa(rows_by_file: Dict[str, List[Dict[str, Any]]], city: str, st: str,
               metro_rows: List[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    """CBSA code of the metro: from the ACS metro row if we have it, else the most common CBSA
    among school districts and colleges located in the principal city."""
    if metro_rows and metro_rows[0].get("cbsa"):
        return str(metro_rows[0]["cbsa"]), "ACS metro row"
    counts: Dict[str, int] = {}
    want = canon(city)
    for kind, city_cols in (("schools", ("city_location", "city_mailing")), ("colleges", ("city",))):
        for r in rows_by_file.get(kind, []):
            state_ok = (r.get("state_location") or r.get("state_mailing") or r.get("state_abbr") or st) == st
            if not state_ok or not r.get("cbsa"):
                continue
            if any(canon(str(r.get(c) or "")) == want for c in city_cols):
                counts[str(r["cbsa"])] = counts.get(str(r["cbsa"]), 0) + 1
    if not counts:
        return None, "no district or college located in the principal city had a CBSA"
    return max(counts, key=lambda k: counts[k]), "most common CBSA among districts/colleges in the principal city"


def filter_metro(rows: List[Dict[str, Any]], cbsa: str) -> List[Dict[str, Any]]:
    return [r for r in rows if str(r.get("cbsa") or "") == cbsa]


def write_metro_files(data: str, states: List[str], city: str, st: str,
                      metro_rows: List[Dict[str, Any]], R: Run) -> None:
    def load(prefix: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for s_ in states:
            path = os.path.join(data, f"{prefix}_{s_}.csv")
            if os.path.exists(path):
                with open(path, encoding="utf-8", newline="") as f:
                    out.extend(csv.DictReader(f))
        return out
    sch, col = load("schools_districts"), load("colleges")
    if not sch and not col:
        raise RuntimeError("No school or college files to filter")
    cbsa, how = metro_cbsa({"schools": sch, "colleges": col}, city, st, metro_rows)
    if not cbsa:
        raise RuntimeError(how)
    msch, mcol = filter_metro(sch, cbsa), filter_metro(col, cbsa)
    note = f"Filtered to CBSA {cbsa} ({how})."
    if msch:
        n = write_csv(os.path.join(data, "schools_districts_metro.csv"), msch)
        R.file("schools_districts_metro.csv", "NCES CCD via Urban Institute API", msch[0].get("year"), n,
               note + " Use this one, not the statewide files.")
    if mcol:
        n = write_csv(os.path.join(data, "colleges_metro.csv"), mcol)
        R.file("colleges_metro.csv", "IPEDS directory via Urban Institute API", mcol[0].get("year"), n,
               note + " No enrollment column.")
    counties: Dict[str, Dict[str, Any]] = {}
    abbr_by_fips = {v[0]: k for k, v in STATES.items()}
    for kind, rows_, fips_col in (("districts", msch, "county_code"), ("colleges", mcol, "county_fips")):
        for r in rows_:
            fips5 = str(r.get(fips_col) or "")
            c = counties.setdefault(fips5, {
                "county_fips": fips5, "state": abbr_by_fips.get(fips5[:2], ""),
                "county_name": r.get("county_name"), "n_districts": 0, "n_colleges": 0})
            c["n_" + kind] += 1
    if counties:
        n = write_csv(os.path.join(data, "metro_counties.csv"), sorted(counties.values(), key=lambda c: c["county_fips"]))
        R.file("metro_counties.csv", "derived from the two files above", "-", n,
               "Counties in the metro that contain a school district or college (not an official delineation list)")


def write_manifest(outdir: str, data: str, city: str, st: str, today: dt.date, R: Run,
                   gaz_year: Optional[int], acs_year: Optional[int]) -> None:
    lines = [
        f"# Data pull for {city}, {st}",
        f"Pulled {today.isoformat()} by scripts/pull_data.py. Files are in this folder.",
        "",
        "## Files",
        "| File | Source | Data year | Rows | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for f in R.files:
        lines.append(f"| {f['file']} | {f['source']} | {f['year']} | {f['rows']} | {f['note']} |")
    if not R.files:
        lines.append("| (none) | | | | Every step failed. See below. |")
    failed = [e for e in R.log if not e["ok"]]
    lines += ["", "## Steps that failed"]
    if any("Census API key" in str(e.get("error", "")) for e in failed):
        lines.append("- **No usable Census API key: ALL Census (ACS) data is missing** (metro totals, "
                     "population, income, age, density, Asian Indian counts). Set CENSUS_API_KEY and rerun, "
                     "or research Module 2 on the web and say so.")
    if failed:
        for e in failed:
            lines.append(f"- {e['step']}: {e.get('error', '')}")
    else:
        lines.append("- None.")
    lines += [
        "",
        "## How to use and cite",
        "- Cite pulled data as (File name, source and data year, A). These are official or primary sources.",
        "- ACS figures are 5-year estimates. Margins of error are included ONLY for population and median "
        "household income (population_moe, median_hh_income_moe; blank for the metro population, which Census "
        "controls). Other fields (age share, Asian Indian counts) have none: say 'about' for small places.",
        "- pop_change_note explains changes over 5% using the 2020 census count (census2020_pop, "
        "change_vs_2020_pct): where the earlier ACS base was far from the census (for example Aurora IL) the "
        "change is an estimate revision, so use change_vs_2020_pct; where the base agrees, it is probably real.",
        "- Place and county files cover whole states. Keep only the metro's counties and the chosen core places.",
        "- Density = ACS population / Census Gazetteer land area (square miles).",
        "- 'pop_change_pct' compares two non-overlapping ACS 5-year periods (earlier_year vs acs_year). "
        "Boundary changes can distort it.",
        "- Asian Indian counts use ACS table B02018 (alone or in any combination). It is a population count, "
        "not a measure of vegetarian demand.",
        "- The weather file is for ONE airport station. Say which one when you cite it.",
        "- poi_counts_osm.csv (only if you ran --poi-only) counts breweries, bars, restaurants, Indian restaurants, "
        "vegetarian/vegan restaurants, colleges and Hindu temples in a circle around each place. Volunteer-mapped: "
        "coverage differs by city. Use them only as proxies for the ranking and label scores built on them low confidence.",
        "- School and college files come from the Urban Institute's Education Data API (NCES/IPEDS). "
        "The college file has no enrollment numbers.",
        "- If a step failed above, say so in the report's open questions and fall back to web research.",
    ]
    with open(os.path.join(data, "MANIFEST.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(data, "pull_log.json"), "w", encoding="utf-8") as f:
        json.dump({"city": f"{city}, {st}", "pulled": today.isoformat(), "acs_year": acs_year,
                   "gazetteer_year": gaz_year, "steps": R.log, "files": R.files}, f, indent=2)


# --------------------------------------------------------------------------
# Self test
# --------------------------------------------------------------------------
def selftest() -> int:
    today = dt.date.today()
    checks: List[Tuple[str, str, bool]] = []

    def check(label: str, url: str, critical: bool) -> None:
        ok, detail = probe(url)
        checks.append((label, f"{'OK  ' if ok else 'FAIL'} {detail}", ok or not critical))
        print(f"{'OK  ' if ok else 'FAIL'}  {label}: {detail}")

    print("Checking that each data source is reachable from this computer...\n")
    acs_ok = False
    key = get_census_key()
    if not key:
        print("FAIL  Census ACS API: no key. " + KEY_HELP)
    else:
        for y in range(today.year - 1, today.year - 6, -1):
            try:
                if census_query(y, "acs/acs5", ["NAME"], "state:17", None, key):
                    acs_ok = True
                    print(f"OK    Census ACS API ({y}) with your key")
                    break
            except CensusKeyError:
                print("FAIL  Census ACS API: key rejected. " + KEY_HELP)
                break
            except Exception:
                continue
        if not acs_ok:
            print("FAIL  Census ACS API: no year responded")
    checks.append(("Census ACS API", "", acs_ok))
    gaz_ok = False
    for y in range(today.year, today.year - 5, -1):
        ok, detail = probe(GAZETTEER.format(y=y))
        if ok:
            gaz_ok = True
            print(f"OK    Census Gazetteer ({y}): {detail}")
            break
    if not gaz_ok:
        print("FAIL  Census Gazetteer: no year responded")
    checks.append(("Gazetteer", "", gaz_ok))
    check("NOAA station list", GHCND_STATIONS, True)
    check("NOAA normals (O'Hare sample)", NCEI_NORMALS.format(sid="USW00094846"), True)
    check("Urban Institute education API",
          f"{URBAN}/school-districts/ccd/directory/{today.year - 3}/?fips=17", False)
    check("Chicago open data (Socrata)",
          "https://data.cityofchicago.org/resource/uupf-x98q.json?$limit=1", False)  # optional
    failed_critical = [c for c in checks if not c[2]]
    print("\nCritical sources (Census, Gazetteer, NOAA) " +
          ("all reachable." if not failed_critical else "have failures: the pull will be incomplete."))
    return 0 if not failed_critical else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Pull public data for a Pizza Karma market-research run.")
    ap.add_argument("city", nargs="?", help='City and state, e.g. "Chicago, IL"')
    ap.add_argument("--places", help="Comma-separated suburbs/places to include in acs_places_core.csv")
    ap.add_argument("--radius", type=float, default=35.0,
                    help="With no --places, only consider places within this many miles of the principal city")
    ap.add_argument("--poi-only", action="store_true",
                    help="Only add OpenStreetMap counts (poi_counts_osm.csv) to an existing pull; slow, run in the background")
    ap.add_argument("--poi-max", type=int, default=13, help="Most places to count with --poi-only")
    ap.add_argument("--neighborhoods-only", action="store_true",
                    help="Only add OpenStreetMap counts around the principal city's neighborhoods "
                         "(neighborhood_counts_osm.csv); slow, run in the background")
    ap.add_argument("--neighborhood-names", help="Comma list of neighborhoods to count instead of the automatic list, "
                                                 'e.g. "Loop,West Loop,Wicker Park"')
    ap.add_argument("--neighborhood-max", type=int, default=25, help="Most neighborhoods to count")
    ap.add_argument("--top", type=int, default=40, help="If --places is omitted, include this many largest places")
    ap.add_argument("--states", help="Override the metro's states, e.g. IL,IN,WI")
    ap.add_argument("--station", help="NOAA station id (e.g. USW00094846) if automatic lookup fails")
    ap.add_argument("--outdir", help="Output folder (default: reports/<city-st>)")
    ap.add_argument("--census-key", help="Optional Census API key (or set CENSUS_API_KEY)")
    ap.add_argument("--no-schools", action="store_true", help="Skip the schools and colleges pull")
    ap.add_argument("--socrata-domain", help="City open-data domain, e.g. data.cityofchicago.org")
    ap.add_argument("--socrata-dataset", help="Socrata dataset id, e.g. uupf-x98q")
    ap.add_argument("--socrata-field", help="Field to count by (default license_description)")
    ap.add_argument("--socrata-search", help="Comma-separated SQL LIKE patterns for the license field (default 'Mobile Food%%')")
    ap.add_argument("--selftest", action="store_true", help="Check that each data source is reachable")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.neighborhoods_only:
        if args.outdir:
            outdir = args.outdir
        elif args.city:
            c, st_ = parse_city_arg(args.city)
            outdir = os.path.join("reports", slugify(c, st_))
        else:
            ap.error('--neighborhoods-only needs the city ("Chicago, IL") or --outdir')
        names = [n.strip() for n in (args.neighborhood_names or "").split(",") if n.strip()]
        return neighborhoods_only(outdir, names or None, args.neighborhood_max)
    if args.poi_only:
        city_arg = args.city
        if not (city_arg or args.outdir):
            ap.error('--poi-only needs the city ("Chicago, IL") or --outdir')
        if args.outdir:
            outdir = args.outdir
        else:
            c, st_ = parse_city_arg(city_arg)
            outdir = os.path.join("reports", slugify(c, st_))
        return poi_only(outdir, args.poi_max)
    if not args.city:
        ap.error('A city is required, e.g. "Chicago, IL" (or use --selftest)')
    try:
        return run(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
