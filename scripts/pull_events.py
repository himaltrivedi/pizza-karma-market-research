#!/usr/bin/env python3
"""
pull_events.py - pull structured EVENT and DEMAND data for a city that has already been through pull_data.py,
so the events and private-demand modules start from real files instead of search summaries.

Standard library only. Run after pull_data.py (it reads data/acs_places_core.csv, metro_counties.csv,
colleges_metro.csv, schools_districts_metro.csv):

  python3 pull_events.py "Chicago, IL"                 # every step
  python3 pull_events.py "Chicago, IL" --steps mlb,bts # chosen steps (tm,mlb,bts,cbp,ipeds,ccd,socrata)
  python3 pull_events.py "Chicago, IL" --dataset data.cityofchicago.org xgse-8eg7   # save one city dataset

Writes to reports/<slug>/data/ (and appends a section to MANIFEST.md):
  events_ticketmaster.csv, events_by_month.csv, venues_top.csv   concerts/sports/theater/family events, next 12 months
                                                                  (needs a free key: TICKETMASTER_API_KEY or ~/.ticketmaster_key)
  sports_home_games.csv, sports_by_month.csv                      MLB home games for teams in the metro (official API, no key)
  airport_monthly_passengers.csv, airport_seasonality_index.csv   BTS passengers by month for the nearest big airports
  business_patterns_county.csv, business_patterns_metro.csv       Census County Business Patterns (uses the Census key)
  campus_enrollment.csv                                           IPEDS fall enrollment + est. graduates per college
  k12_seniors.csv                                                 CCD grade-12 enrollment per district (est. graduates)
  city_open_data_candidates.csv                                   the city's own events / permits / markets datasets

Every step is independent; a failure is logged and the rest still run.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import os
import re
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("pull_data", os.path.join(_here, "pull_data.py"))
pd = importlib.util.module_from_spec(_spec)          # reuse http_get, Run, write_csv, key lookup, ...
_spec.loader.exec_module(pd)  # type: ignore[union-attr]

TM_HELP = ("No Ticketmaster key: set TICKETMASTER_API_KEY or save the key in ~/.ticketmaster_key "
           "(free at https://developer.ticketmaster.com/)")
TM_SEGMENTS = ["Music", "Sports", "Arts & Theatre", "Miscellaneous", "Film"]
TM_PAGE = 200
TM_DEEP_LIMIT = 1000          # the API refuses size x page beyond this

CBP_NAICS = [
    ("722330", "Mobile food services"), ("722320", "Caterers"), ("722410", "Drinking places (bars)"),
    ("722511", "Full-service restaurants"), ("722513", "Limited-service restaurants"),
    ("721110", "Hotels and motels"), ("7111", "Performing arts companies"), ("7112", "Spectator sports"),
    ("7113", "Promoters of performing arts, sports and similar events"),
    ("71", "Arts, entertainment and recreation (all)"), ("00", "All industries"),
]

SOCRATA_DOMAINS = {
    "chicago-il": "data.cityofchicago.org", "austin-tx": "data.austintexas.gov", "seattle-wa": "data.seattle.gov",
    "new-york-ny": "data.cityofnewyork.us", "los-angeles-ca": "data.lacity.org", "san-francisco-ca": "data.sfgov.org",
    "dallas-tx": "www.dallasopendata.com", "nashville-tn": "data.nashville.gov", "kansas-city-mo": "data.kcmo.org",
    "cincinnati-oh": "data.cincinnati-oh.gov",
}
SOCRATA_TERMS = ["special events", "street festival", "farmers market", "food truck", "event permit", "block party"]


# --------------------------------------------------------------------------
# Inputs from the earlier pull
# --------------------------------------------------------------------------
def read_csv(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def fnum(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def load_context(data: str) -> Dict[str, Any]:
    core = read_csv(os.path.join(data, "acs_places_core.csv"))
    if not core or fnum(core[0].get("lat")) is None:
        raise RuntimeError("data/acs_places_core.csv (with lat/lon) is missing: run pull_data.py with a Census key first")
    p = core[0]
    counties = {r["county_fips"] for r in read_csv(os.path.join(data, "metro_counties.csv")) if r.get("county_fips")}
    return {
        "name": p["name"], "state": p["state"], "lat": float(p["lat"]), "lon": float(p["lon"]),
        "counties": counties,
        "colleges": {r["unitid"]: r for r in read_csv(os.path.join(data, "colleges_metro.csv")) if r.get("unitid")},
        "districts": {r["leaid"]: r for r in read_csv(os.path.join(data, "schools_districts_metro.csv"))
                      if r.get("leaid")},
    }


def get_ticketmaster_key(cli: Optional[str] = None) -> Optional[str]:
    key = cli or os.environ.get("TICKETMASTER_API_KEY")
    if not key:
        try:
            with open(os.path.join(os.path.expanduser("~"), ".ticketmaster_key"), encoding="utf-8") as f:
                key = f.read().strip() or None
        except OSError:
            key = None
    return key.strip() if key else None


def month_key(d: str) -> str:
    return d[:7]


# --------------------------------------------------------------------------
# Ticketmaster Discovery API
# --------------------------------------------------------------------------
def tm_url(key: str, lat: float, lon: float, radius: float, start: dt.date, end: dt.date,
           segment: str, page: int) -> str:
    q = {"apikey": key, "latlong": f"{lat:.4f},{lon:.4f}", "radius": f"{radius:g}", "unit": "miles",
         "startDateTime": start.strftime("%Y-%m-%dT00:00:00Z"), "endDateTime": end.strftime("%Y-%m-%dT23:59:59Z"),
         "segmentName": segment, "size": str(TM_PAGE), "page": str(page), "sort": "date,asc",
         "includeTest": "no", "includeTBA": "no"}
    return "https://app.ticketmaster.com/discovery/v2/events.json?" + urllib.parse.urlencode(q)


def tm_parse(event: Dict[str, Any]) -> Dict[str, Any]:
    cl = (event.get("classifications") or [{}])[0]
    venue = ((event.get("_embedded") or {}).get("venues") or [{}])[0]
    loc = venue.get("location") or {}
    return {
        "id": event.get("id"), "name": event.get("name"),
        "date": (event.get("dates", {}).get("start", {}) or {}).get("localDate"),
        "time": (event.get("dates", {}).get("start", {}) or {}).get("localTime"),
        "segment": (cl.get("segment") or {}).get("name"), "genre": (cl.get("genre") or {}).get("name"),
        "venue": venue.get("name"), "venue_city": (venue.get("city") or {}).get("name"),
        "lat": loc.get("latitude"), "lon": loc.get("longitude"),
    }


def tm_fetch_range(key: str, lat: float, lon: float, radius: float, start: dt.date, end: dt.date, segment: str,
                   out: Dict[str, Dict[str, Any]], pause: float = 0.25) -> None:
    """All events of one segment in [start, end]. A range with more than the API's deep-paging cap is
    split in half until it fits."""
    first = json.loads(pd.http_get(tm_url(key, lat, lon, radius, start, end, segment, 0), retries=2, timeout=40))
    total = int((first.get("page") or {}).get("totalElements") or 0)
    if total == 0:
        return
    if total > TM_DEEP_LIMIT and end > start:
        mid = start + (end - start) // 2
        tm_fetch_range(key, lat, lon, radius, start, mid, segment, out, pause)
        tm_fetch_range(key, lat, lon, radius, mid + dt.timedelta(days=1), end, segment, out, pause)
        return
    pages = min(int((first.get("page") or {}).get("totalPages") or 1), TM_DEEP_LIMIT // TM_PAGE)
    for pg in range(pages):
        data = first if pg == 0 else json.loads(
            pd.http_get(tm_url(key, lat, lon, radius, start, end, segment, pg), retries=2, timeout=40))
        for ev in (data.get("_embedded") or {}).get("events", []):
            row = tm_parse(ev)
            if row["id"]:
                out[row["id"]] = row
        time.sleep(pause)


def month_ranges(start: dt.date, months: int) -> List[Tuple[dt.date, dt.date]]:
    out = []
    y, m = start.year, start.month
    for i in range(months):
        first = dt.date(y, m, 1) if i else start
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        last = dt.date(ny, nm, 1) - dt.timedelta(days=1)
        out.append((first, last))
        y, m = ny, nm
    return out


def pull_ticketmaster(key: str, lat: float, lon: float, radius: float, start: dt.date, months: int,
                      pause: float = 0.25) -> List[Dict[str, Any]]:
    events: Dict[str, Dict[str, Any]] = {}
    for a, b in month_ranges(start, months):
        for seg in TM_SEGMENTS:
            tm_fetch_range(key, lat, lon, radius, a, b, seg, events, pause)
    return sorted(events.values(), key=lambda r: (r["date"] or "", r["name"] or ""))


VENUE_MONTH_CAP = 15      # one venue counts at most this many listings per month (kept in step with event_inventory.py)


def events_by_month(rows: List[Dict[str, Any]], venue_cap: Optional[int] = None) -> List[Dict[str, Any]]:
    """Listings per month and segment. With `venue_cap`, a venue counts at most that many listings a month, so a
    studio or ticket reseller with thousands of listings cannot make a quiet month look busy."""
    counts: Dict[Tuple[str, str], int] = {}
    per_venue: Dict[Tuple[str, str], int] = {}
    for r in rows:
        if r.get("date"):
            mk = month_key(r["date"])
            if venue_cap is not None and r.get("venue"):
                vk = (r["venue"], mk)
                per_venue[vk] = per_venue.get(vk, 0) + 1
                if per_venue[vk] > venue_cap:
                    continue
            k = (mk, r.get("segment") or "Other")
            counts[k] = counts.get(k, 0) + 1
    return [{"month": m, "segment": s, "events": n} for (m, s), n in sorted(counts.items())]


def venues_top(rows: List[Dict[str, Any]], n: int = 25) -> List[Dict[str, Any]]:
    c: Dict[Tuple[str, str], int] = {}
    for r in rows:
        if r.get("venue"):
            c[(r["venue"], r.get("venue_city") or "")] = c.get((r["venue"], r.get("venue_city") or ""), 0) + 1
    return [{"venue": v, "city": ct, "events": k} for (v, ct), k in sorted(c.items(), key=lambda kv: -kv[1])[:n]]


# --------------------------------------------------------------------------
# MLB (official API, no key)
# --------------------------------------------------------------------------
MLB = "https://statsapi.mlb.com/api/v1"


def mlb_home_games(lat: float, lon: float, radius: float, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    teams = json.loads(pd.http_get(f"{MLB}/teams?sportId=1&hydrate=venue(location)"))["teams"]
    rows: List[Dict[str, Any]] = []
    for t in teams:
        v = t.get("venue") or {}
        c = ((v.get("location") or {}).get("defaultCoordinates") or {})
        if "latitude" not in c or pd.haversine_miles(lat, lon, c["latitude"], c["longitude"]) > radius:
            continue
        url = (f"{MLB}/schedule?sportId=1&teamId={t['id']}&startDate={start.isoformat()}&endDate={end.isoformat()}"
               "&gameType=R,F,D,L,W")
        for d in json.loads(pd.http_get(url)).get("dates", []):
            for g in d.get("games", []):
                if (g.get("venue") or {}).get("id") == v.get("id"):
                    away = ((g.get("teams") or {}).get("away") or {}).get("team", {}).get("name")
                    rows.append({"date": d["date"], "team": t["name"], "opponent": away, "venue": v.get("name"),
                                 "game_type": g.get("gameType")})
    return sorted(rows, key=lambda r: r["date"])


def sports_by_month(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    c: Dict[Tuple[str, str], int] = {}
    for r in rows:
        k = (month_key(r["date"]), r["team"])
        c[k] = c.get(k, 0) + 1
    return [{"month": m, "team": t, "home_games": n} for (m, t), n in sorted(c.items())]


# --------------------------------------------------------------------------
# Airports and BTS passengers (no key)
# --------------------------------------------------------------------------
OURAIRPORTS = "https://davidmegginson.github.io/ourairports-data/airports.csv"
BTS = "https://data.bts.gov/resource/r495-tyji.json"


def airports_near(text: str, lat: float, lon: float, max_miles: float = 40.0, limit: int = 3) -> List[Dict[str, Any]]:
    out = []
    for r in csv.DictReader(text.splitlines()):
        if r.get("iso_country") != "US" or r.get("type") not in ("large_airport", "medium_airport"):
            continue
        if r.get("scheduled_service") != "yes" or not r.get("iata_code"):
            continue
        la, lo = fnum(r.get("latitude_deg")), fnum(r.get("longitude_deg"))
        if la is None or lo is None:
            continue
        d = pd.haversine_miles(lat, lon, la, lo)
        if d <= max_miles:
            out.append({"iata": r["iata_code"], "name": r["name"], "type": r["type"], "miles": round(d, 1)})
    out.sort(key=lambda a: (a["type"] != "large_airport", a["miles"]))
    return out[:limit]


def bts_month_rows(iata: str, first_year: int) -> List[Dict[str, Any]]:
    params = {"$select": "reporting_month,sum(total_passengers) as pax",
              "$where": f"origin_airport_code='{iata}' AND year>='{first_year}'",
              "$group": "reporting_month", "$order": "reporting_month", "$limit": "500"}
    url = BTS + "?" + urllib.parse.urlencode(params)
    return [{"airport": iata, "month": r["reporting_month"][:7], "passengers": int(float(r["pax"]))}
            for r in json.loads(pd.http_get(url))]


def seasonality_index(rows: List[Dict[str, Any]], years: int = 3) -> List[Dict[str, Any]]:
    """Calendar-month index (100 = an average month) from the last `years` COMPLETE calendar years,
    all airports added together."""
    by_month: Dict[str, int] = {}
    for r in rows:
        by_month[r["month"]] = by_month.get(r["month"], 0) + r["passengers"]
    complete = sorted(y for y in {m[:4] for m in by_month}
                      if all(f"{y}-{mm:02d}" in by_month and by_month[f"{y}-{mm:02d}"] > 0 for mm in range(1, 13)))
    use = complete[-years:]
    if not use:
        return []
    avg = {mm: sum(by_month[f"{y}-{mm:02d}"] for y in use) / len(use) for mm in range(1, 13)}
    overall = sum(avg.values()) / 12
    return [{"month": mm, "avg_passengers": round(avg[mm]), "index": round(avg[mm] / overall * 100, 1),
             "years_used": ",".join(use)} for mm in range(1, 13)]


# --------------------------------------------------------------------------
# Census County Business Patterns (Census key)
# --------------------------------------------------------------------------
def cbp_query(year: int, state_fips: str, naics: str, key: str) -> List[Dict[str, str]]:
    """One County Business Patterns call: every county of a state for one NAICS code."""
    if not key:
        raise pd.CensusKeyError(pd.KEY_HELP)
    url = (f"https://api.census.gov/data/{year}/cbp?get=NAME,ESTAB,EMP,NAICS2017&for=county:*"
           f"&in=state:{state_fips}&NAICS2017={naics}&key={key}")
    raw = pd.http_get(url)
    try:
        data = json.loads(raw)
    except ValueError:
        if b"key" in raw[:2000].lower():
            raise pd.CensusKeyError(pd.KEY_HELP)
        raise RuntimeError(f"Census returned non-JSON for cbp {year}: {raw[:100]!r}")
    return [dict(zip(data[0], r)) for r in data[1:]]


def cbp_pull(key: str, counties: Set[str], today: dt.date) -> Tuple[List[Dict[str, Any]], int]:
    states = sorted({c[:2] for c in counties})
    last: Any = None
    for year in range(today.year - 2, today.year - 6, -1):
        rows: List[Dict[str, Any]] = []
        try:
            for st in states:
                for code, label in CBP_NAICS:
                    for r in cbp_query(year, st, code, key):
                        fips = r["state"] + r["county"]
                        if fips in counties:
                            rows.append({"county_fips": fips, "county": r["NAME"], "naics": code, "industry": label,
                                         "establishments": int(r["ESTAB"]), "employees": int(r["EMP"])})
            if rows:
                return rows, year
        except pd.CensusKeyError:
            raise
        except Exception as e:      # this year not published, or the industry code is not in this vintage
            last = e
    raise RuntimeError(f"No County Business Patterns year worked (last error: {last})")


def cbp_metro_totals(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    t: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        x = t.setdefault(r["naics"], {"naics": r["naics"], "industry": r["industry"], "establishments": 0,
                                      "employees": 0, "counties": 0})
        x["establishments"] += r["establishments"]
        x["employees"] += r["employees"]
        x["counties"] += 1
    return [t[c] for c, _ in CBP_NAICS if c in t]


# --------------------------------------------------------------------------
# IPEDS enrollment and CCD seniors (Urban Institute API, no key)
# --------------------------------------------------------------------------
def urban_year(path_tpl: str, params: Dict[str, Any], today: dt.date) -> Tuple[List[Dict[str, Any]], int]:
    last: Any = None
    for y in range(today.year - 2, today.year - 6, -1):
        try:
            rows = pd.urban_fetch_all(path_tpl.format(year=y), params)
            if rows:
                return rows, y
        except pd.NetworkError:
            raise
        except Exception as e:
            last = e
    raise RuntimeError(f"No year worked for {path_tpl} (last error: {last})")


def ipeds_enrollment(fips_list: List[str], colleges: Dict[str, Dict[str, str]], today: dt.date):
    out: List[Dict[str, Any]] = []
    year = None
    for fips in fips_list:
        rows, year = urban_year(
            "college-university/ipeds/fall-enrollment/{year}/99/99/99/",
            {"fips": fips, "sex": 99, "race": 99, "ftpt": 99, "class_level": 99, "degree_seeking": 99}, today)
        for r in rows:
            c = colleges.get(str(r.get("unitid")))
            e = r.get("enrollment_fall")
            if c and isinstance(e, (int, float)) and e >= 0:
                out.append({"unitid": r["unitid"], "name": c.get("inst_name"), "city": c.get("city"),
                            "county": c.get("county_name"), "fall_enrollment": int(e), "year": year,
                            "degree_granting": c.get("degree_granting"),
                            "est_graduates_per_year": round(e / 4)})
    out.sort(key=lambda r: -r["fall_enrollment"])
    return out, year


def ccd_seniors(fips_list: List[str], districts: Dict[str, Dict[str, str]], today: dt.date):
    out: List[Dict[str, Any]] = []
    year = None
    for fips in fips_list:
        rows, year = urban_year("school-districts/ccd/enrollment/{year}/grade-12/", {"fips": fips}, today)
        for r in rows:
            d = districts.get(str(r.get("leaid")))
            e = r.get("enrollment")
            if d and r.get("race") == 99 and r.get("sex") == 99 and isinstance(e, (int, float)) and e >= 0:
                out.append({"leaid": r["leaid"], "district": d.get("lea_name"), "city": d.get("city_location"),
                            "county": d.get("county_name"), "grade12_enrollment": int(e), "year": year,
                            "agency_type": d.get("agency_type")})
    out.sort(key=lambda r: -r["grade12_enrollment"])
    return out, year


# --------------------------------------------------------------------------
# City open data: find the datasets (Socrata catalog)
# --------------------------------------------------------------------------
CATALOG = "https://api.us.socrata.com/api/catalog/v1"
# datasets about requests, complaints or city operations, not about events
NOT_EVENTS = re.compile(r"foia|request log|311|complaint|vehicle|crime|violation|inspection|tow|parking ticket|"
                        r"permit application log|budget|salary|employee|contract|lobby", re.I)


def socrata_candidates(slug: str, city: str, domain: Optional[str]) -> List[Dict[str, Any]]:
    domain = domain or SOCRATA_DOMAINS.get(slug)
    words = [w for w in re.split(r"[^a-z]+", city.lower()) if len(w) > 2]
    seen: Dict[str, Dict[str, Any]] = {}
    for term in SOCRATA_TERMS:
        q = {"q": term if domain else f"{city} {term}", "only": "datasets", "limit": "10"}
        if domain:
            q["domains"] = domain
        data = json.loads(pd.http_get(CATALOG + "?" + urllib.parse.urlencode(q), retries=1, timeout=40))
        for item in data.get("results", []):
            res, meta = item.get("resource", {}), item.get("metadata", {})
            dom = meta.get("domain", "")
            if not domain and not any(w in dom.lower() for w in words):
                continue
            if res.get("id") in seen:
                continue
            cols = res.get("columns_name") or []
            seen[res["id"]] = {
                "term": term, "domain": dom, "id": res["id"], "name": res.get("name"),
                "updated": (res.get("data_updated_at") or res.get("updatedAt") or "")[:10],
                "has_date_column": any(re.search(r"date|start|time", c, re.I) for c in cols),
                "columns": "; ".join(cols[:14]), "description": re.sub(r"\s+", " ", res.get("description") or "")[:160]}
    rows = [r for r in seen.values() if not NOT_EVENTS.search(r["name"] or "")]
    this_year = dt.date.today().year
    for r in rows:
        r["stale"] = bool(r["updated"]) and this_year - int(r["updated"][:4]) > 3
    # useful first: has a date column, current, then newest
    rows.sort(key=lambda r: (r["stale"], not r["has_date_column"], -int((r["updated"] or "0000")[:4] or 0)))
    return rows


def save_dataset(domain: str, dataset: str, path: str, page: int = 5000, cap: int = 50000) -> int:
    rows: List[Dict[str, Any]] = []
    while len(rows) < cap:
        url = f"https://{domain}/resource/{dataset}.json?" + urllib.parse.urlencode(
            {"$limit": page, "$offset": len(rows), "$order": ":id"})
        chunk = json.loads(pd.http_get(url))
        rows.extend(chunk)
        if len(chunk) < page:
            break
    flat = [{k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items() if not k.startswith(":")}
            for r in rows]
    return pd.write_csv(path, flat)


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
ALL_STEPS = ["tm", "mlb", "bts", "cbp", "ipeds", "ccd", "socrata"]


def append_manifest(data: str, R, today: dt.date) -> None:
    lines = ["", f"## Events and demand data (pull_events.py, {today.isoformat()})",
             "| File | Source | Data year | Rows | Notes |", "| --- | --- | --- | --- | --- |"]
    for f in R.files:
        lines.append(f"| {f['file']} | {f['source']} | {f['year']} | {f['rows']} | {f['note']} |")
    failed = [e for e in R.log if not e["ok"]]
    if failed:
        lines += ["", "Steps that did not produce data:"] + [f"- {e['step']}: {e.get('error', '')}" for e in failed]
    lines += ["", "How to use: these are inputs for Modules 3, 4, 5, 6 and 7. Counts of events and people may be sized "
              "from them as estimates (est.) with the method shown; they are not revenue figures."]
    path = os.path.join(data, "MANIFEST.md")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(data, "events_pull_log.json"), "w", encoding="utf-8") as f:
        json.dump({"pulled": today.isoformat(), "steps": R.log, "files": R.files}, f, indent=2)


def run(args: argparse.Namespace) -> int:
    city, st = pd.parse_city_arg(args.city)
    slug = pd.slugify(city, st)
    outdir = args.outdir or os.path.join("reports", slug)
    data = os.path.join(outdir, "data")
    today = dt.date.today()
    steps = [s.strip() for s in (args.steps or ",".join(ALL_STEPS)).split(",") if s.strip()]

    if args.dataset:
        dom, ds = args.dataset
        n = save_dataset(dom, ds, os.path.join(data, f"city_{ds}.csv"))
        print(f"Saved {n} rows to {os.path.join(data, f'city_{ds}.csv')}")
        return 0 if n else 1

    R = pd.Run()
    ctx = R.attempt("Read the earlier pull", lambda: load_context(data))
    if not ctx:
        print("Run pull_data.py first (with a Census key).", file=sys.stderr)
        return 2
    lat, lon = ctx["lat"], ctx["lon"]
    key = get_ticketmaster_key(args.tm_key)
    fips_states = sorted({c[:2] for c in ctx["counties"]}) or [pd.STATES[st][0]]
    print(f"Pulling events and demand data for {city}, {st} ({', '.join(steps)})")

    if "tm" in steps:
        if not key:
            R.log.append({"step": "Ticketmaster events", "ok": False, "error": "skipped: " + TM_HELP})
        else:
            def tm() -> int:
                rows = pull_ticketmaster(key, lat, lon, args.radius, today, args.months)
                n = pd.write_csv(os.path.join(data, "events_ticketmaster.csv"), rows,
                                 ["id", "name", "date", "time", "segment", "genre", "venue", "venue_city", "lat", "lon"])
                R.file("events_ticketmaster.csv", "Ticketmaster Discovery API", today.year, n,
                       f"Ticketed events within {args.radius:g} miles, next {args.months} months. Free community, "
                       "cultural and street events are mostly NOT here: research those on the web")
                pd.write_csv(os.path.join(data, "events_by_month.csv"), events_by_month(rows, VENUE_MONTH_CAP))
                pd.write_csv(os.path.join(data, "venues_top.csv"), venues_top(rows))
                R.file("events_by_month.csv", "derived from events_ticketmaster.csv", today.year, len(events_by_month(rows, VENUE_MONTH_CAP)),
                       f"events per month and segment; one venue counts at most {VENUE_MONTH_CAP} listings a month")
                R.file("venues_top.csv", "derived from events_ticketmaster.csv", today.year, len(venues_top(rows)),
                       "venues with the most ticketed events")
                return n
            R.attempt("Ticketmaster events", tm)

    if "mlb" in steps:
        def mlb() -> int:
            rows = mlb_home_games(lat, lon, args.radius, today, today + dt.timedelta(days=365))
            if not rows:
                raise RuntimeError("no MLB team plays within the radius, or no schedule is published yet")
            n = pd.write_csv(os.path.join(data, "sports_home_games.csv"), rows)
            R.file("sports_home_games.csv", "MLB Stats API (statsapi.mlb.com)", today.year, n,
                   "Home games only, next 12 months. NBA/NHL/NFL/MLS come from Ticketmaster or web research")
            pd.write_csv(os.path.join(data, "sports_by_month.csv"), sports_by_month(rows))
            return n
        R.attempt("MLB home games", mlb)

    if "bts" in steps:
        def bts() -> int:
            ap = airports_near(pd.http_get(OURAIRPORTS, timeout=90).decode("utf-8", "replace"), lat, lon)
            if not ap:
                raise RuntimeError("no scheduled-service airport within 40 miles")
            rows: List[Dict[str, Any]] = []
            for a in ap:
                rows += bts_month_rows(a["iata"], today.year - 5)
            n = pd.write_csv(os.path.join(data, "airport_monthly_passengers.csv"), rows)
            idx = seasonality_index(rows)
            pd.write_csv(os.path.join(data, "airport_seasonality_index.csv"), idx)
            R.file("airport_monthly_passengers.csv", "BTS T-100 (data.bts.gov)", today.year, n,
                   "Departing passengers by month, airports: " + ", ".join(f"{a['iata']} ({a['miles']} mi)" for a in ap)
                   + ". Residents and visitors together: a proxy for visitor seasonality, not a visitor count")
            R.file("airport_seasonality_index.csv", "derived (last 3 complete years)", today.year, len(idx),
                   "Index 100 = an average month")
            return n
        R.attempt("Airport passengers by month", bts)

    if "cbp" in steps:
        def cbp() -> int:
            k = pd.get_census_key(args.census_key)
            if not ctx["counties"]:
                raise RuntimeError("data/metro_counties.csv is missing")
            rows, y = cbp_pull(k, ctx["counties"], today)
            n = pd.write_csv(os.path.join(data, "business_patterns_county.csv"), rows)
            tot = cbp_metro_totals(rows)
            pd.write_csv(os.path.join(data, "business_patterns_metro.csv"), tot)
            R.file("business_patterns_county.csv", "Census County Business Patterns", y, n,
                   "Establishments and employees by county and industry (suppressed values show as 0)")
            R.file("business_patterns_metro.csv", "derived (sum over the metro counties listed in metro_counties.csv)", y,
                   len(tot), "Only counties that contain a school district or college are included")
            return n
        R.attempt("County Business Patterns", cbp)

    if "ipeds" in steps:
        def ipeds() -> int:
            rows, y = ipeds_enrollment(fips_states, ctx["colleges"], today)
            n = pd.write_csv(os.path.join(data, "campus_enrollment.csv"), rows)
            R.file("campus_enrollment.csv", "IPEDS fall enrollment via Urban Institute API", y, n,
                   "est_graduates_per_year = fall enrollment / 4 (an estimate, label est.). Online-heavy schools "
                   "(for example DeVry, Chamberlain, Phoenix) report huge enrollments that are not on a local campus: "
                   "use traditional campuses for graduation-week timing")
            return n
        R.attempt("Campus enrollment", ipeds)

    if "ccd" in steps:
        def ccd() -> int:
            rows, y = ccd_seniors(fips_states, ctx["districts"], today)
            n = pd.write_csv(os.path.join(data, "k12_seniors.csv"), rows)
            R.file("k12_seniors.csv", "NCES CCD via Urban Institute API", y, n,
                   "Grade-12 enrollment: a proxy for graduating seniors (est.). Includes charter and online schools")
            return n
        R.attempt("K-12 seniors", ccd)

    if "socrata" in steps:
        def soc() -> int:
            rows = socrata_candidates(slug, city, args.socrata_domain)
            if not rows:
                raise RuntimeError("no city open-data datasets found (the city may not use Socrata)")
            n = pd.write_csv(os.path.join(data, "city_open_data_candidates.csv"), rows)
            R.file("city_open_data_candidates.csv", "Socrata open-data catalog", today.year, n,
                   "Datasets that may list events, permits, markets or food trucks. Pick useful ones and save with "
                   "`pull_events.py CITY --dataset DOMAIN ID`")
            return n
        R.attempt("City open-data catalog", soc)

    append_manifest(data, R, today)
    ok = sum(1 for e in R.log if e["ok"])
    print(f"\nDone: {ok} steps ok, {len(R.log) - ok} not done. See {os.path.join(data, 'MANIFEST.md')}")
    return 0 if R.files else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Pull event and demand data for a Pizza Karma market-research run.")
    ap.add_argument("city", help='City and state, e.g. "Chicago, IL"')
    ap.add_argument("--outdir", help="Output folder (default: reports/<city-st>)")
    ap.add_argument("--steps", help=f"Comma list of steps (default all): {','.join(ALL_STEPS)}")
    ap.add_argument("--radius", type=float, default=35.0, help="Miles around the principal city (default 35)")
    ap.add_argument("--months", type=int, default=12, help="Months of events to pull (default 12)")
    ap.add_argument("--tm-key", help="Ticketmaster key (or TICKETMASTER_API_KEY, or ~/.ticketmaster_key)")
    ap.add_argument("--census-key", help="Census key (or CENSUS_API_KEY, or ~/.census_key)")
    ap.add_argument("--socrata-domain", help="City open-data domain, e.g. data.cityofchicago.org")
    ap.add_argument("--dataset", nargs=2, metavar=("DOMAIN", "ID"), help="Save one city dataset and exit")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
