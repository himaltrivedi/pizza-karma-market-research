#!/usr/bin/env python3
"""
classify_names.py - count a license file's businesses by cuisine from their NAMES, with a visible keyword list, and
print the names it could not classify so they can be looked up (instead of guessing).

  python3 classify_names.py data/chicago_business_licenses_mobile_food_rows.csv \\
        [--name-col doing_business_as_name] [--where license_description="Mobile Food License"] \\
        [--activity-col business_activity] [--out data/name_classification.csv] [--show-unclassified 60]

The categories and keywords are the table CUISINES below: first match wins, in that order. This is a reading of
business names, not menus: a taco-named truck may sell burgers. Say that in the report. Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from typing import Dict, List, Optional, Tuple

# Ordered: first matching category wins. Keywords are matched as whole words or word starts, case-insensitive.
CUISINES: List[Tuple[str, List[str]]] = [
    ("pizza", ["pizza", "pizzeria", "pie", "pies", "calzone", "brick oven", "wood fired", "wood-fired"]),
    ("indian or south asian", ["indian", "desi", "tandoor", "tandoori", "masala", "curry", "biryani", "naan", "samosa",
                               "tikka", "chaat", "dosa", "punjabi", "lahori", "karahi", "paneer", "bombay", "delhi",
                               "mumbai", "chai", "paratha", "pakistani", "bangladeshi", "nepal", "momo", "kebab roll"]),
    ("halal or middle eastern", ["halal", "shawarma", "shawarman", "falafel", "gyro", "gyros", "mediterranean", "kabob",
                                 "kebab", "hummus", "lebanese", "turkish", "persian", "greek", "arab", "pita"]),
    ("mexican or latin", ["taco", "tacos", "taqueria", "burrito", "mexican", "mexico", "mexi", "carnitas", "pupusa",
                          "pupuseria", "baleada", "baleadas", "latin", "latino", "arepa", "arepas", "empanada",
                          "empanadas", "birria", "quesadilla", "tamal", "tamales", "elote", "torta", "tortas",
                          "cubano", "cuban", "paisa", "bunuelo", "bunuelos", "fritos", "caseras", "casero", "colombian", "venezuelan", "salvadoran", "honduran", "guatemalan",
                          "peruvian", "caribbean", "jerk", "plantain", "tostada", "al pastor"]),
    ("dessert and sweets", ["ice cream", "icecream", "paleta", "paleteria", "paletas", "cupcake", "cupcakes", "bakery",
                            "donut", "donuts", "doughnut", "cookie", "cookies", "sweet", "sweets", "dessert", "gelato",
                            "frozen", "churro", "churros", "waffle", "waffles", "crepe", "crepes", "candy", "cake",
                            "cakes", "pastry", "pastries", "treats", "sundae", "custard", "popsicle", "sorbet",
                            "italian ice", "shaved ice", "snow cone", "cannoli", "creamery"]),
    ("coffee and drinks", ["coffee", "cafe", "café", "espresso", "latte", "tea", "boba", "juice", "smoothie",
                           "lemonade", "brew", "kombucha", "roast"]),
    ("seafood", ["lobster", "seafood", "fish", "shrimp", "crab", "oyster", "clam", "poke"]),
    ("asian", ["sushi", "ramen", "thai", "korean", "chinese", "wok", "pho", "banh", "dumpling", "bao", "teriyaki",
               "japanese", "vietnamese", "filipino", "hibachi", "noodle", "noodles", "bibimbap", "kimchi", "asian"]),
    ("vegan or vegetarian", ["vegan", "vegetarian", "plant based", "plant-based", "veggie", "salad", "salads"]),
    ("burgers, bbq and american", ["burger", "burgers", "bbq", "barbecue", "barbeque", "smokehouse", "smoke", "smoked",
                                   "hot dog", "hotdog", "dogs", "sausage", "brat", "wings", "wing", "chicken", "fried",
                                   "steak", "cheesesteak", "philly", "sandwich", "sandwiches", "sub", "subs", "grill",
                                   "grilled", "mac", "fries", "melt", "melts", "po boy", "po'boy", "bbq", "ribs",
                                   "beef", "diner", "deli", "soul", "southern", "cajun", "creole", "comfort"]),
]


SPANISH_PREFIX = re.compile(r"^(el|la|los|las)\s")


def normalize(name: str) -> str:
    n = re.sub(r"\b(vin|pl)\b\s*[#:]?.*$", " ", (name or "").lower())       # "...  VIN#1GB... PL#365" noise
    n = re.sub(r"[^a-z0-9' ]+", " ", n)
    n = re.sub(r"\b(llc|inc|corp|co|ltd|the)\b\.?", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def classify(name: str) -> Tuple[str, str]:
    """(category, matched keyword). category 'unclassified' when nothing matches."""
    n = " " + normalize(name).replace("'", "") + " "
    spanish = bool(SPANISH_PREFIX.match(n.strip()))
    for cat, words in CUISINES:
        for w in words:
            w2 = w.replace("'", "")
            if re.search(r"(?<![a-z0-9])" + re.escape(w2) + r"(?![a-z0-9])", n) or \
                    (len(w2) >= 5 and re.search(r"(?<![a-z0-9])" + re.escape(w2), n)):
                return cat, w
    if spanish:
        return "mexican or latin", "Spanish article (El/La/Los/Las): weak signal"
    return "unclassified", ""


def load(path: str, where: Optional[str]) -> List[Dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if where:
        col, _, val = where.partition("=")
        if col not in (rows[0] if rows else {}):
            sys.exit(f"--where column {col!r} not in the file. Columns: {', '.join(list(rows[0])[:30])}")
        rows = [r for r in rows if r.get(col, "").strip().lower() == val.strip().strip('"').lower()]
    return rows


def summarize(rows: List[Dict[str, str]], name_col: str) -> Tuple[Dict[str, Dict], List[Dict[str, str]]]:
    """Per category: license rows, distinct business names, example names. Plus the per-row result."""
    out: Dict[str, Dict] = {}
    detail: List[Dict[str, str]] = []
    for r in rows:
        name = (r.get(name_col) or "").strip()
        cat, kw = classify(name)
        d = out.setdefault(cat, {"rows": 0, "names": set(), "examples": []})
        d["rows"] += 1
        key = normalize(name)
        if key and key not in d["names"]:
            d["names"].add(key)
            if len(d["examples"]) < 4:
                d["examples"].append(name.title())
        detail.append({"name": name, "category": cat, "matched_keyword": kw})
    return out, detail


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--name-col", default="doing_business_as_name")
    ap.add_argument("--where", help='keep rows where COLUMN=VALUE, e.g. license_description="Mobile Food License"')
    ap.add_argument("--activity-col", help="also count rows by this column (for example business_activity)")
    ap.add_argument("--out", help="write the per-row result (name, category, matched keyword) to this CSV")
    ap.add_argument("--show-unclassified", type=int, default=60, help="how many unclassified names to print")
    a = ap.parse_args(argv)
    rows = load(a.file, a.where)
    if not rows:
        print("No rows (check --where).")
        return 1
    if a.name_col not in rows[0]:
        sys.exit(f"Column {a.name_col!r} not in the file. Columns: {', '.join(list(rows[0])[:30])}")
    if a.activity_col:
        counts: Dict[str, int] = {}
        for r in rows:
            counts[r.get(a.activity_col, "") or "(blank)"] = counts.get(r.get(a.activity_col, "") or "(blank)", 0) + 1
        print(f"Rows by {a.activity_col}:")
        for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n}\t{k}")
        print()
    summary, detail = summarize(rows, a.name_col)
    total = len(rows)
    print(f"Cuisine by business NAME (rough: names, not menus). {total} rows, "
          f"{len({normalize(r.get(a.name_col, '')) for r in rows})} distinct names.\n")
    print("| Category | Rows | Distinct names | Share of rows | Examples |")
    print("| --- | --- | --- | --- | --- |")
    order = [c for c, _ in CUISINES] + ["unclassified"]
    for cat in order:
        d = summary.get(cat)
        if d:
            print(f"| {cat} | {d['rows']} | {len(d['names'])} | {d['rows'] / total * 100:.0f}% | {'; '.join(d['examples'][:3])} |")
    un = summary.get("unclassified", {"rows": 0})
    print(f"\nClassified by name: {total - un['rows']} of {total} rows ({(total - un['rows']) / total * 100:.0f}%). "
          "State this and that the grouping is a reading of names.")
    names = sorted({r["name"] for r in detail if r["category"] == "unclassified" and r["name"]})
    if names and a.show_unclassified:
        print(f"\nUnclassified names ({len(names)}); look up the ones that matter, do not guess:")
        for n in names[: a.show_unclassified]:
            print("  -", n)
        if len(names) > a.show_unclassified:
            print(f"  ... {len(names) - a.show_unclassified} more (use --out to save all)")
    if a.out:
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["name", "category", "matched_keyword"])
            w.writeheader()
            w.writerows(detail)
        print(f"\nWrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
