#!/usr/bin/env python3
"""
competitors.py - one table for the pizza and Indian/South Asian vendors (trucks, restaurants, caterers), so prices,
ratings and review counts are recorded in a fixed shape and gaps are visible. Standard library only.

  python3 competitors.py add --data DIR --name "Spicy Desi" --cuisine "indian or south asian" --kind truck \\
        --platform "Roaming Hunger" --price-item "main" --price-low 12 --price-high 16 --rating 4.6 --reviews 38 \\
        --booking "catering form" --oven unknown --source "https://..." --confidence B
  python3 competitors.py coverage --data DIR    who has prices / ratings / reviews, who is missing what, next lookups
  python3 competitors.py report   --data DIR    markdown: vendor table + price range per item type with menus sampled

Prices are the vendor's own menu or a named listing; competitor prices are RANGES (low to high per item type) and the
number of menus sampled is reported. --data defaults to reports/<slug>/data for a city like "Chicago, IL".
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from typing import Dict, List, Optional

FIELDS = ["name", "cuisine", "kind", "area", "platform", "price_item", "price_low", "price_high", "rating",
          "review_count", "booking", "oven", "source", "confidence"]
CUISINES = ["pizza", "indian or south asian", "indian-fusion pizza", "other"]
KINDS = ["truck", "restaurant", "caterer"]
OVEN = ["yes", "no", "unknown"]
CONFIDENCE = ["A", "B", "C"]
PIZZA = ("pizza", "indian-fusion pizza")
INDIAN = ("indian or south asian", "indian-fusion pizza")


def read_rows(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def num(v: object) -> Optional[float]:
    try:
        return float(str(v).replace("$", "").replace(",", "")) if str(v).strip() != "" else None
    except ValueError:
        return None


def validate(r: Dict[str, str]) -> List[str]:
    e = []
    if not r["name"].strip():
        e.append("name is required")
    if r["cuisine"] not in CUISINES:
        e.append(f"cuisine must be one of: {'; '.join(CUISINES)}")
    if r["kind"] not in KINDS:
        e.append(f"kind must be one of: {', '.join(KINDS)}")
    if r["oven"] not in OVEN:
        e.append("oven (tandoor or wood-fired oven on the vehicle/premises) must be yes, no or unknown")
    if r["confidence"] not in CONFIDENCE:
        e.append("confidence must be A, B or C")
    lo, hi = num(r["price_low"]), num(r["price_high"])
    for k in ("price_low", "price_high", "rating", "review_count"):
        if r[k].strip() and num(r[k]) is None:
            e.append(f"{k} must be a number")
    if (lo is None) != (hi is None):
        e.append("give both price_low and price_high (a range), or neither")
    elif lo is not None and hi is not None:
        if lo > hi:
            e.append("price_low is higher than price_high")
        if not r["price_item"].strip():
            e.append("price_item is required with a price (for example main, pie, appetizer, drink, per person)")
    if r["rating"].strip() and num(r["rating"]) is not None and not 0 <= num(r["rating"]) <= 10:
        e.append("rating must be between 0 and 10 (say the platform's scale in --platform)")
    if r["rating"].strip() and not r["platform"].strip():
        e.append("name the --platform the rating comes from")
    return e


CARRY = ("area", "platform", "booking", "source")


def upsert(rows: List[Dict[str, str]], row: Dict[str, str]) -> List[Dict[str, str]]:
    """One vendor, no duplicate rows. A priced row replaces the vendor's placeholder (no price) row and keeps its
    blank-filled details; a row with no price merges its non-empty details into the vendor's existing row; the same
    vendor and price item replaces the old row."""
    key = row["name"].strip().lower()
    mine = [r for r in rows if r["name"].strip().lower() == key]
    priced = bool(row["price_item"].strip())
    for r in mine:
        if priced and r["price_item"].strip().lower() == row["price_item"].strip().lower():
            rows[rows.index(r)] = row
            return rows
    if not mine:
        rows.append(row)
        return rows
    if priced:
        placeholders = [r for r in mine if not r["price_item"].strip()]
        for ph in placeholders:
            for k in CARRY + ("rating", "review_count"):
                if not row[k].strip() and ph[k].strip():
                    row[k] = ph[k]
            if row["oven"] == "unknown" and ph["oven"] != "unknown":
                row["oven"] = ph["oven"]
            rows.remove(ph)
        rows.append(row)
        return rows
    base = mine[0]                       # no price on the new row: fold its details into the vendor's first row
    for k in CARRY + ("rating", "review_count"):
        if row[k].strip():
            base[k] = row[k]
    if row["oven"] != "unknown":
        base["oven"] = row["oven"]
    if row["confidence"] < base["confidence"]:        # A beats B beats C
        base["confidence"] = row["confidence"]
    return rows


def cmd_add(path: str, a: argparse.Namespace) -> int:
    row = {"name": a.name, "cuisine": a.cuisine, "kind": a.kind, "area": a.area or "", "platform": a.platform or "",
           "price_item": a.price_item or "", "price_low": a.price_low or "", "price_high": a.price_high or "",
           "rating": a.rating or "", "review_count": a.reviews or "", "booking": a.booking or "", "oven": a.oven,
           "source": a.source or "", "confidence": a.confidence}
    errs = validate(row)
    if errs:
        print("NOT ADDED:\n- " + "\n- ".join(errs))
        return 2
    rows = read_rows(path)
    rows = upsert(rows, row)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"Saved: {row['name']} ({row['cuisine']}, {row['kind']})")
    return 0


def vendors(rows: List[Dict[str, str]]) -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    for r in rows:
        v = out.setdefault(r["name"], {"cuisine": r["cuisine"], "kind": r["kind"], "prices": 0, "rating": False,
                                       "reviews": False, "booking": False, "oven": "unknown"})
        v["prices"] += 1 if (num(r["price_low"]) is not None) else 0
        v["rating"] = v["rating"] or num(r["rating"]) is not None
        v["reviews"] = v["reviews"] or num(r["review_count"]) is not None
        v["booking"] = v["booking"] or bool(r["booking"].strip())
        if r["oven"] != "unknown":
            v["oven"] = r["oven"]
    return out


def cmd_coverage(path: str) -> int:
    rows = read_rows(path)
    v = vendors(rows)
    pz = {n: d for n, d in v.items() if d["cuisine"] in PIZZA}
    ind = {n: d for n, d in v.items() if d["cuisine"] in INDIAN}
    print(f"Vendors recorded: {len(v)}  (pizza {len(pz)} of up to 8; Indian/South Asian {len(ind)} of up to 5)")
    for label, group in (("Pizza", pz), ("Indian / South Asian", ind)):
        if not group:
            continue
        print(f"\n{label}:")
        print("vendor\tprice\trating\treviews\tbooking\toven")
        for n, d in sorted(group.items()):
            print(f"{n}\t{'yes' if d['prices'] else 'MISSING'}\t{'yes' if d['rating'] else 'MISSING'}\t"
                  f"{'yes' if d['reviews'] else 'MISSING'}\t{'yes' if d['booking'] else 'MISSING'}\t{d['oven']}")
    todo = []
    if len(pz) < 3:
        todo.append("find more pizza vendors (trucks, restaurants, caterers): search the truck directories and the "
                    "unclassified license names")
    if len(ind) < 3:
        todo.append("find more Indian / South Asian vendors; check halal and Pakistani-named licenses")
    for n, d in sorted({**pz, **ind}.items()):
        missing = [k for k, ok in (("a menu price", d["prices"]), ("a rating", d["rating"]),
                                   ("a review count", d["reviews"]), ("how it takes bookings", d["booking"])) if not ok]
        if missing:
            todo.append(f"{n}: still need {', '.join(missing)}")
    print("\nNext lookups:\n- " + "\n- ".join(todo[:15]) if todo else "\nEvery recorded vendor has a price, rating, reviews and booking method.")
    if not any(d["oven"] == "yes" for d in {**pz, **ind}.values()):
        print("\nNo vendor is recorded with a tandoor or wood-fired oven: say how many you checked, not that none exist.")
    return 0


def cmd_report(path: str) -> int:
    rows = read_rows(path)
    if not rows:
        print("(no vendors recorded: use `competitors.py add`)")
        return 0
    print("### Pizza and Indian / South Asian vendors\n")
    print("| Vendor | Type | Cuisine | Where | Price (item: range) | Rating (platform, reviews) | Bookings | Oven | Source |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for n, grp in _by_name(rows).items():
        r0 = grp[0]
        prices = "; ".join(f"{r['price_item']}: ${num(r['price_low']):g}-${num(r['price_high']):g}"
                           for r in grp if num(r["price_low"]) is not None) or "Not found"
        rating = next((r for r in grp if num(r["rating"]) is not None), None)
        rt = f"{rating['rating']} ({rating['platform']}, {rating['review_count'] or 'n/a'} reviews)" if rating else "Not found"
        oven = next((r["oven"] for r in grp if r["oven"] != "unknown"), "unknown")
        conf = "/".join(sorted({r["confidence"] for r in grp}))
        print(f"| {n} | {r0['kind']} | {r0['cuisine']} | {r0['area'] or '-'} | {prices} | {rt} | "
              f"{r0['booking'] or 'Not found'} | {oven} | {r0['source'] or '-'} ({conf}) |")
    print("\n### Price ranges by item type (low to high across vendors; menus sampled = vendors with a price)\n")
    print("| Group | Item | Low | High | Menus sampled |")
    print("| --- | --- | --- | --- | --- |")
    for label, cuisines in (("Pizza", PIZZA), ("Indian / South Asian", INDIAN)):
        items: Dict[str, List[Dict[str, str]]] = {}
        for r in rows:
            if r["cuisine"] in cuisines and num(r["price_low"]) is not None:
                items.setdefault(re.split(r"\s*[(;]", r["price_item"])[0].strip().lower(), []).append(r)
        for item, rs in sorted(items.items()):
            lo = min(num(r["price_low"]) for r in rs)
            hi = max(num(r["price_high"]) for r in rs)
            print(f"| {label} | {item} | ${lo:g} | ${hi:g} | {len({r['name'] for r in rs})} |")
        if not items:
            print(f"| {label} | - | Not found | Not found | 0 |")
    return 0


def _by_name(rows: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        out.setdefault(r["name"], []).append(r)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("add", "coverage", "report"):
        p = sub.add_parser(name)
        p.add_argument("city", nargs="?")
        p.add_argument("--data")
        if name == "add":
            p.add_argument("--name", required=True)
            p.add_argument("--cuisine", required=True)
            p.add_argument("--kind", required=True)
            p.add_argument("--area")
            p.add_argument("--platform")
            p.add_argument("--price-item", dest="price_item")
            p.add_argument("--price-low", dest="price_low")
            p.add_argument("--price-high", dest="price_high")
            p.add_argument("--rating")
            p.add_argument("--reviews")
            p.add_argument("--booking")
            p.add_argument("--oven", default="unknown")
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
        data = os.path.join("reports", re.sub(r"[^a-z0-9]+", "-", f"{m.group(1)}-{m.group(2)}".lower()).strip("-"), "data")
    path = os.path.join(data, "competitors.csv")
    return {"add": lambda: cmd_add(path, a), "coverage": lambda: cmd_coverage(path),
            "report": lambda: cmd_report(path)}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
