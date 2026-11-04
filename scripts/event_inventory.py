#!/usr/bin/env python3
"""
event_inventory.py - keep the events research in one fixed table, see what is still missing, and build the
Module 3 / 4 / 6 tables from it. Standard library only. Works on reports/<slug>/data/.

  python3 event_inventory.py init     CITY|--data DIR      create data/event_inventory.csv (header only)
  python3 event_inventory.py add      --data DIR --name "Taste of Chicago" --category festival --month "Jun-Jul" \\
                                      --venue "Grant Park" --size 25k+ --trucks yes --south-asian n \\
                                      --source "https://..." --confidence B
  python3 event_inventory.py coverage --data DIR      events per month and category; thin months; missing categories
  python3 event_inventory.py report   --data DIR      Module 3 tables (monthly calendar, categories, top 10 picks)
  python3 event_inventory.py demand   --data DIR      Module 4/6 month-by-month demand index (est.) with its method
  python3 event_inventory.py neighborhoods --data DIR  scored top 10 neighborhoods of the principal city (OpenStreetMap
                                                      counts from `pull_data.py --neighborhoods-only`)

`--data` defaults to reports/<slug>/data when given a city like "Chicago, IL".
Inputs it reads when present: event_inventory.csv (your rows), events_ticketmaster.csv, sports_home_games.csv,
airport_seasonality_index.csv, noaa_monthly_normals.csv, acs_metro.csv, campus_enrollment.csv, k12_seniors.csv.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

FIELDS = ["name", "category", "typical_month", "neighborhood_or_venue", "size_tier", "food_trucks_allowed",
          "south_asian", "source", "confidence"]
CATEGORIES = ["food festival", "music", "sports", "arts and theater", "farmers or night market",
              "cultural or south asian", "convention or trade show", "brewery or venue series",
              "holiday market", "college or campus", "community fair or parade", "other"]
SIZE_TIERS = ["<1k", "1-5k", "5-25k", "25k+", "unknown"]
TRUCKS = ["yes", "no", "unknown"]
CONFIDENCE = ["A", "B", "C", "est."]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_NAMES = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}
MONTH_NAMES.update({n: i + 1 for i, n in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"])})
MONTH_NAMES["sept"] = 9
# Ticketmaster segments and MLB mapped to our categories
SEGMENT_CATEGORY = {"music": "music", "sports": "sports", "arts & theatre": "arts and theater",
                    "film": "arts and theater", "miscellaneous": "other"}
THIN = 3          # fewer recurring events than this in a month = thin
VENUE_MONTH_CAP = 15   # one venue counts at most this many ticketed listings a month (same as pull_events.py)
MIN_LARGE_EVENTS = 8   # fewer than this many 5k+ events listed means the major-event search is not finished
MIN_INVENTORY_FOR_INDEX = 12   # the events column of the demand index needs at least this many listed events

# Rough national patterns (shares of a year, in percent) used ONLY for est. shapes; replace with county data when found.
WEDDING_SHAPE_NATIONAL = [3, 4, 5, 7, 11, 13, 9, 9, 14, 15, 7, 3]
WEDDING_SHAPE_SOUTH_ASIAN = [8, 7, 6, 9, 9, 6, 4, 4, 5, 8, 18, 16]
GRADUATION_SHAPE = [0, 0, 1, 2, 45, 32, 3, 5, 1, 1, 1, 9]
MARRIAGES_PER_1000 = 6.0       # about 6 marriages per 1,000 residents per year in the US (CDC; check the latest year)
SIZE_POINTS = {"<1k": 0, "1-5k": 1, "5-25k": 2, "25k+": 3, "unknown": 0}
TRUCK_POINTS = {"yes": 2, "unknown": 1, "no": -5}
CONF_POINTS = {"A": 1.0, "B": 0.5, "C": 0.0, "est.": 0.0}


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


# --------------------------------------------------------------------------
# Months
# --------------------------------------------------------------------------
def parse_months(text: str) -> List[int]:
    """'June' | '6' | '2027-06' | 'May-Oct' | 'Jun; Sep' | 'weekly' | 'year-round' -> month numbers (1-12)."""
    t = (text or "").strip().lower()
    if not t:
        return []
    if re.search(r"year[- ]?round|weekly|monthly|all year|every month", t):
        return list(range(1, 13))
    out: List[int] = []
    for part in re.split(r"[;,/&]| and ", t):
        part = part.strip()
        m = re.fullmatch(r"(\d{4})-(\d{1,2})", part)
        if m:
            out.append(int(m.group(2)))
            continue
        rng = re.fullmatch(r"([a-z]+|\d{1,2})\s*(?:-|–|to|through)\s*([a-z]+|\d{1,2})", part)
        if rng:
            a, b = _one(rng.group(1)), _one(rng.group(2))
            if a and b:
                i = a
                while True:
                    out.append(i)
                    if i == b:
                        break
                    i = i % 12 + 1
                continue
        one = _one(part)
        if one:
            out.append(one)
    return sorted(set(m for m in out if 1 <= m <= 12))


def _one(tok: str) -> Optional[int]:
    tok = tok.strip().lower().rstrip(".")
    if tok.isdigit():
        n = int(tok)
        return n if 1 <= n <= 12 else None
    return MONTH_NAMES.get(tok) or MONTH_NAMES.get(tok[:3])


# --------------------------------------------------------------------------
# Loading everything into per-month structures
# --------------------------------------------------------------------------
def load_inventory(data: str) -> List[Dict[str, str]]:
    return read_csv(os.path.join(data, "event_inventory.csv"))


def ticketed_by_month(data: str) -> Dict[int, Dict[str, int]]:
    """month -> {category: number of dated events} from Ticketmaster and MLB home games."""
    out: Dict[int, Dict[str, int]] = {m: {} for m in range(1, 13)}
    per_venue: Dict[Tuple[str, int], int] = {}
    for r in read_csv(os.path.join(data, "events_ticketmaster.csv")):
        d = r.get("date") or ""
        if len(d) >= 7 and d[5:7].isdigit():
            cat = SEGMENT_CATEGORY.get((r.get("segment") or "").lower(), "other")
            m = int(d[5:7])
            if r.get("venue"):
                per_venue[(r["venue"], m)] = per_venue.get((r["venue"], m), 0) + 1
                if per_venue[(r["venue"], m)] > VENUE_MONTH_CAP:
                    continue
            out[m][cat] = out[m].get(cat, 0) + 1
    for r in read_csv(os.path.join(data, "sports_home_games.csv")):
        d = r.get("date") or ""
        if len(d) >= 7 and d[5:7].isdigit():
            m = int(d[5:7])
            out[m]["sports"] = out[m].get("sports", 0) + 1
    return out


def recurring_by_month(rows: List[Dict[str, str]]) -> Dict[int, List[Dict[str, str]]]:
    out: Dict[int, List[Dict[str, str]]] = {m: [] for m in range(1, 13)}
    for r in rows:
        for m in parse_months(r.get("typical_month", "")):
            out[m].append(r)
    return out


# --------------------------------------------------------------------------
# init / add
# --------------------------------------------------------------------------
def cmd_init(data: str) -> int:
    path = os.path.join(data, "event_inventory.csv")
    os.makedirs(data, exist_ok=True)
    if os.path.exists(path):
        print(f"{path} already exists ({len(read_csv(path))} rows): not touched")
        return 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(FIELDS)
    print(f"Created {path}")
    return 0


def validate_row(r: Dict[str, str]) -> List[str]:
    errs = []
    if not r.get("name", "").strip():
        errs.append("name is required")
    if r.get("category") not in CATEGORIES:
        errs.append(f"category must be one of: {'; '.join(CATEGORIES)}")
    if not parse_months(r.get("typical_month", "")):
        errs.append("typical_month not understood (use 'June', 'Jun-Aug', 'May; Sep', '2027-06', 'weekly' or 'year-round')")
    if r.get("size_tier") not in SIZE_TIERS:
        errs.append(f"size must be one of: {', '.join(SIZE_TIERS)}")
    if r.get("food_trucks_allowed") not in TRUCKS:
        errs.append(f"trucks must be one of: {', '.join(TRUCKS)}")
    if r.get("south_asian") not in ("y", "n"):
        errs.append("south-asian must be y or n")
    if r.get("confidence") not in CONFIDENCE:
        errs.append(f"confidence must be one of: {', '.join(CONFIDENCE)}")
    return errs


def cmd_add(data: str, a: argparse.Namespace) -> int:
    row = {"name": a.name, "category": a.category, "typical_month": a.month, "neighborhood_or_venue": a.venue or "",
           "size_tier": a.size, "food_trucks_allowed": a.trucks, "south_asian": a.south_asian,
           "source": a.source or "", "confidence": a.confidence}
    errs = validate_row(row)
    if errs:
        print("NOT ADDED:\n- " + "\n- ".join(errs))
        return 2
    path = os.path.join(data, "event_inventory.csv")
    exists = os.path.exists(path)
    existing = {(r["name"].lower(), r["typical_month"].lower()) for r in read_csv(path)}
    if (row["name"].lower(), row["typical_month"].lower()) in existing:
        print(f"Already listed: {row['name']} ({row['typical_month']}): not added again")
        return 0
    os.makedirs(data, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)
    print(f"Added: {row['name']} ({row['typical_month']}, {row['category']})")
    return 0


# --------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------
def coverage(data: str) -> Dict[str, Any]:
    inv = load_inventory(data)
    rec = recurring_by_month(inv)
    tick = ticketed_by_month(data)
    per_month = {m: len(rec[m]) for m in range(1, 13)}
    thin = [MONTHS[m - 1] for m in range(1, 13) if per_month[m] < THIN]
    have = {r["category"] for r in inv}
    missing = [c for c in CATEGORIES if c != "other" and c not in have]
    sa = sum(1 for r in inv if r.get("south_asian") == "y")
    large = sum(1 for r in inv if r.get("size_tier") in ("25k+", "5-25k"))
    text = " ".join(f"{r.get('name', '')} {r.get('neighborhood_or_venue', '')}" for r in inv).lower()
    core = [r["name"] for r in read_csv(os.path.join(data, "acs_places_core.csv"))]
    no_event_places = [n for n in core if n.lower() not in text]
    return {"large_events": large, "places_without_event": no_event_places, "rows": len(inv), "per_month": per_month, "ticketed": {m: sum(tick[m].values()) for m in range(1, 13)},
            "thin_months": thin, "missing_categories": missing, "south_asian_rows": sa,
            "food_truck_unknown": sum(1 for r in inv if r.get("food_trucks_allowed") == "unknown"),
            "ticketed_present": any(sum(tick[m].values()) for m in tick)}


def cmd_coverage(data: str) -> int:
    c = coverage(data)
    print(f"Recurring events in event_inventory.csv: {c['rows']}  (South Asian/cultural rows: {c['south_asian_rows']}, "
          f"food-truck rule unknown: {c['food_truck_unknown']})")
    print(f"Ticketed events pulled (Ticketmaster + MLB): {'yes' if c['ticketed_present'] else 'NONE (no key or not run)'}")
    print("\nmonth\trecurring\tticketed")
    for m in range(1, 13):
        flag = "  <-- thin" if c["per_month"][m] < THIN else ""
        print(f"{MONTHS[m - 1]}\t{c['per_month'][m]}\t{c['ticketed'][m]}{flag}")
    print(f"Large events listed (5k+ crowd): {c['large_events']} (aim for at least {MIN_LARGE_EVENTS})")
    print("Core places with no event or venue named in the inventory: " + (", ".join(c["places_without_event"]) or "none"))
    print("\nThin months (fewer than %d recurring events): %s" % (THIN, ", ".join(c["thin_months"]) or "none"))
    print("Categories with no events yet: " + (", ".join(c["missing_categories"]) or "none"))
    todo = []
    if c["large_events"] < MIN_LARGE_EVENTS:
        todo.append("major events: search the largest annual events by attendance for the principal city (festivals, "
                    "parades, marathons, fairs, big venues and arenas), then 'largest annual festival <suburb>' and "
                    "'<suburb> events attendance' for each core suburb below; record each with its venue or suburb")
    if c["places_without_event"]:
        todo.append("no event listed for: " + ", ".join(c["places_without_event"][:12]) +
                    " (one search each: '<place> annual festival', village events page, county fair, park district)")
    if c["thin_months"]:
        todo.append("thin months (" + ", ".join(c["thin_months"]) + "): one search per month, e.g. '<month> events "
                    "festivals markets <city>', aimed at free, community and holiday events")
    for cat in c["missing_categories"]:
        todo.append(f"search for {cat} events in the metro")
    if c["south_asian_rows"] == 0:
        todo.insert(1 if c["thin_months"] else 0,
                    "South Asian calendar: Diwali, Holi, Navratri/Garba, India Independence Day events and melas")
    if todo:
        print("\nNext searches:\n- " + "\n- ".join(todo[:12]))
    else:
        print("\nCoverage is good: every month has enough events and every category has at least one.")
    return 0


# --------------------------------------------------------------------------
# report (Module 3 tables)
# --------------------------------------------------------------------------
def pick_score(r: Dict[str, str]) -> float:
    return (SIZE_POINTS.get(r.get("size_tier", "unknown"), 0) + TRUCK_POINTS.get(r.get("food_trucks_allowed", "unknown"), 1)
            + (1 if r.get("south_asian") == "y" else 0) + CONF_POINTS.get(r.get("confidence", "C"), 0)
            + min(len(parse_months(r.get("typical_month", ""))), 3) * 0.1)


def pick_reason(r: Dict[str, str]) -> str:
    bits = [f"crowd {r.get('size_tier', 'unknown')}"]
    t = r.get("food_trucks_allowed")
    bits.append("trucks allowed" if t == "yes" else "truck rule unknown" if t == "unknown" else "trucks not allowed")
    if r.get("south_asian") == "y":
        bits.append("South Asian audience")
    return ", ".join(bits)


def cmd_report(data: str) -> int:
    inv = load_inventory(data)
    rec = recurring_by_month(inv)
    tick = ticketed_by_month(data)
    print("### Monthly calendar (generated from data/event_inventory.csv and the pulled ticketed events)\n")
    print("| Month | Recurring events (up to 6) | Ticketed events (count) | Sports home games |")
    print("| --- | --- | --- | --- |")
    for m in range(1, 13):
        names = "; ".join(f"{r['name']} ({r['confidence']})" for r in rec[m][:6]) or "Not found"
        extra = f" +{len(rec[m]) - 6} more" if len(rec[m]) > 6 else ""
        print(f"| {MONTHS[m - 1]} | {names}{extra} | {sum(tick[m].values()) or '-'} | {tick[m].get('sports', '-')} |")
    print("\n### Event categories\n")
    print("| Category | Events listed | Food trucks allowed (yes / unknown / no) |")
    print("| --- | --- | --- |")
    for cat in CATEGORIES:
        rows = [r for r in inv if r["category"] == cat]
        y = sum(1 for r in rows if r["food_trucks_allowed"] == "yes")
        u = sum(1 for r in rows if r["food_trucks_allowed"] == "unknown")
        n = sum(1 for r in rows if r["food_trucks_allowed"] == "no")
        print(f"| {cat} | {len(rows)} | {y} / {u} / {n} |")
    print("\n### Top 10 picks (scored: crowd size, truck rule, South Asian audience, source quality)\n")
    print("| # | Event | When | Why |")
    print("| --- | --- | --- | --- |")
    ranked, seen = [], set()
    for r in sorted(inv, key=lambda r: (-pick_score(r), r["name"])):
        series = r["name"].split("(")[0].strip().lower()      # one row per event series, not one per month
        if series in seen:
            continue
        seen.add(series)
        ranked.append(r)
    ranked = ranked[:10]
    for i, r in enumerate(ranked, 1):
        print(f"| {i} | {r['name']} | {r['typical_month']} | {pick_reason(r)} |")
    if not inv:
        print("\n(no rows yet: add events with `event_inventory.py add`)")
    return 0


# --------------------------------------------------------------------------
# demand (Module 4 / 6 tables, est.)
# --------------------------------------------------------------------------
def norm100(vals: Dict[int, float]) -> Dict[int, float]:
    top = max(vals.values()) if vals else 0
    return {m: (v / top * 100 if top else 0.0) for m, v in vals.items()}


def weather_workable(data: str) -> Dict[int, float]:
    out: Dict[int, float] = {}
    days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    for r in read_csv(os.path.join(data, "noaa_monthly_normals.csv")):
        m = int(r["month"])
        warm, rain = fnum(r.get("days_high_ge_50f")), fnum(r.get("days_precip_ge_0.10in"))
        if warm is not None:
            out[m] = warm * (1 - (rain or 0) / days[m - 1])
    return out


def cmd_demand(data: str) -> int:
    metro = (read_csv(os.path.join(data, "acs_metro.csv")) or [{}])[0]
    pop, sa_pct = fnum(metro.get("population")), fnum(metro.get("asian_indian_pct_of_pop"))
    comps: Dict[str, Tuple[float, Dict[int, float]]] = {}
    inv_rec = recurring_by_month(load_inventory(data))
    tick = ticketed_by_month(data)
    events = {m: len(inv_rec[m]) + sum(tick[m].values()) / 5.0 for m in range(1, 13)}   # 5 ticketed ~ 1 recurring
    inv_rows = len(load_inventory(data))
    has_ticketmaster = bool(read_csv(os.path.join(data, "events_ticketmaster.csv")))
    skipped = ""
    if inv_rows >= MIN_INVENTORY_FOR_INDEX or has_ticketmaster:
        comps["events"] = (0.30, norm100(events))
    else:
        skipped = ("The `events` column is left out: there is no real calendar yet (fewer than %d rows in "
                   "event_inventory.csv and no Ticketmaster file; sports home games alone would make winter look empty)."
                   % MIN_INVENTORY_FOR_INDEX)
    air = {int(r["month"]): float(r["index"]) for r in read_csv(os.path.join(data, "airport_seasonality_index.csv"))}
    if len(air) == 12:
        comps["visitors (airport)"] = (0.15, norm100(air))
    wk = weather_workable(data)
    if len(wk) == 12:
        comps["workable weather"] = (0.25, norm100(wk))
    share = (sa_pct or 0) / 100
    wed = {m + 1: (1 - share) * WEDDING_SHAPE_NATIONAL[m] + share * WEDDING_SHAPE_SOUTH_ASIAN[m] for m in range(12)}
    comps["weddings (est. shape)"] = (0.20, norm100(wed))
    comps["graduations (est. shape)"] = (0.10, norm100({m + 1: float(GRADUATION_SHAPE[m]) for m in range(12)}))
    tw = sum(w for w, _ in comps.values())
    print("### Month-by-month demand index (est.)\n")
    print("| Month | " + " | ".join(f"{k} ({w / tw * 100:.0f}%)" for k, (w, _) in comps.items()) + " | Demand index (0-100) |")
    print("| --- |" + " --- |" * (len(comps) + 1))
    index = {}
    for m in range(1, 13):
        index[m] = sum(w * v[m] for w, v in comps.values()) / tw
        print(f"| {MONTHS[m - 1]} | " + " | ".join(f"{v[m]:.0f}" for _, v in comps.values()) + f" | {index[m]:.0f} |")
    if skipped:
        print("\n" + skipped)
    top = sorted(index, key=lambda m: -index[m])
    print(f"\nStrongest months by this index: {', '.join(MONTHS[m - 1] for m in top[:4])}; "
          f"weakest: {', '.join(MONTHS[m - 1] for m in top[-3:])}.")
    print("\nmethod: each column is scaled so its best month = 100, then combined with the weights shown (weights "
          "are renormalized over the columns available). `events` = recurring events in event_inventory.csv plus "
          "one fifth of the ticketed events pulled; `workable weather` = days with a high of 50 F or more x "
          "(1 - share of days with 0.10 in or more rain), NOAA normals; `visitors` = BTS airport passengers "
          "seasonality; the wedding and graduation columns are rough national shapes, not local counts "
          "(wedding shape blends the national pattern with a South Asian pattern by the metro's Asian Indian share). "
          "Replace them with county clerk or campus data if found. Label everything above est.")
    if pop:
        weddings = pop * MARRIAGES_PER_1000 / 1000
        line = f"\nest. weddings per year in the metro: about {weddings:,.0f} (population {pop:,.0f} x {MARRIAGES_PER_1000:g} per 1,000)"
        if sa_pct:
            line += f"; South Asian weddings if the same rate applied: about {weddings * sa_pct / 100:,.0f} (Asian Indian share {sa_pct:g}%)"
        print(line + ". method: national rate applied to the metro, not a county clerk count.")
    camp = read_csv(os.path.join(data, "campus_enrollment.csv"))
    if camp:
        trad = [r for r in camp if r.get("degree_granting") == "1"]
        g = sum(int(float(r["est_graduates_per_year"])) for r in trad if r.get("est_graduates_per_year"))
        print(f"est. college graduates per year (degree-granting campuses in the metro): about {g:,} "
              "(fall enrollment / 4; online-heavy schools inflate this). method: IPEDS enrollment / 4.")
    k12 = read_csv(os.path.join(data, "k12_seniors.csv"))
    if k12:
        s = sum(int(float(r["grade12_enrollment"])) for r in k12 if r.get("grade12_enrollment"))
        print(f"est. high-school seniors (grade-12 enrollment, metro districts incl. charters): about {s:,}. "
              "method: NCES CCD grade-12 enrollment as a proxy for graduates.")
    return 0


# --------------------------------------------------------------------------
# neighborhoods (principal city), scored with the same quintile rubric as the suburb ranking
# --------------------------------------------------------------------------
def quintile_scores(values: List[float]) -> List[int]:
    """1-5 by quintile of rank within the list; ties share the average rank."""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    import math
    return [max(1, min(5, math.ceil(r / n * 5))) for r in ranks]


def score_neighborhoods(rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    num = lambda r, k: fnum(r.get(k)) or 0.0          # noqa: E731
    s2 = [num(r, "breweries") + num(r, "bars_pubs") for r in rows]
    s3 = [num(r, "colleges_universities") for r in rows]
    s4 = [num(r, "restaurants") + num(r, "bars_pubs") for r in rows]
    q2, q3, q4 = quintile_scores(s2), quintile_scores(s3), quintile_scores(s4)
    out = []
    for i, r in enumerate(rows):
        out.append({"name": r["name"], "S2": q2[i], "S3": q3[i], "S4": q4[i], "total": round((q2[i] + q3[i] + q4[i]) / 3, 2),
                    "breweries_bars": int(s2[i]), "colleges": int(s3[i]), "restaurants_bars": int(s4[i])})
    out.sort(key=lambda d: (-d["total"], -d["restaurants_bars"], d["name"]))
    return out


def cmd_neighborhoods(data: str) -> int:
    rows = read_csv(os.path.join(data, "neighborhood_counts_osm.csv"))
    if not rows:
        print("No neighborhood_counts_osm.csv yet: run `pull_data.py CITY --neighborhoods-only` (slow, background).")
        return 1
    scored = score_neighborhoods(rows)
    print(f"### Neighborhood ranking, principal city ({len(rows)} OpenStreetMap neighborhoods counted; low confidence)\n")
    print("| Rank | Neighborhood | S2 breweries+bars | S3 colleges | S4 restaurants+bars | Total | Evidence (1.5 km circle) |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for i, d in enumerate(scored[:10], 1):
        print(f"| {i} | {d['name']} | {d['S2']} | {d['S3']} | {d['S4']} | {d['total']:.2f} | "
              f"{d['breweries_bars']} breweries+bars, {d['colleges']} colleges, {d['restaurants_bars']} restaurants+bars |")
    print(f"\nNot in the top 10: {', '.join(d['name'] for d in scored[10:]) or 'none'}.")
    print("\nmethod: each signal is scored 1-5 by quintile of rank among the neighborhoods counted (ties share the "
          "average rank); total = the mean of the three; ties go to more restaurants+bars, then name. There is NO "
          "population or income signal here (the pass counts places, not people), circles of neighboring "
          "neighborhoods overlap, and OpenStreetMap undercounts some areas: label every score low confidence and "
          "override a rank with a verified fact (a known entertainment district) when they conflict.")
    return 0


# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "add", "coverage", "report", "demand", "neighborhoods"):
        p = sub.add_parser(name)
        p.add_argument("city", nargs="?", help='City like "Chicago, IL" (finds reports/<slug>/data)')
        p.add_argument("--data", help="Path to the data folder (default reports/<slug>/data)")
        if name == "add":
            p.add_argument("--name", required=True)
            p.add_argument("--category", required=True)
            p.add_argument("--month", required=True)
            p.add_argument("--venue")
            p.add_argument("--size", default="unknown")
            p.add_argument("--trucks", default="unknown")
            p.add_argument("--south-asian", default="n")
            p.add_argument("--source")
            p.add_argument("--confidence", default="C")
    a = ap.parse_args(argv)
    data = a.data
    if not data:
        if not a.city:
            ap.error('give a city ("Chicago, IL") or --data DIR')
        m = re.match(r"^\s*(.+?)[,\s]+([A-Za-z]{2})\s*$", a.city)
        if not m:
            ap.error('city must look like "Chicago, IL"')
        slug = re.sub(r"[^a-z0-9]+", "-", f"{m.group(1)}-{m.group(2)}".lower()).strip("-")
        data = os.path.join("reports", slug, "data")
    if a.cmd == "init":
        return cmd_init(data)
    if a.cmd == "add":
        return cmd_add(data, a)
    if a.cmd == "coverage":
        return cmd_coverage(data)
    if a.cmd == "report":
        return cmd_report(data)
    if a.cmd == "neighborhoods":
        return cmd_neighborhoods(data)
    return cmd_demand(data)


if __name__ == "__main__":
    sys.exit(main())
