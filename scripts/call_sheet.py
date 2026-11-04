#!/usr/bin/env python3
"""
call_sheet.py - the questions only an office can answer, kept in one table and printed as a ready-to-use call sheet.

Some answers are not on any web page: whether a fire marshal will approve a tandoor on a truck, a plan-review fee that
is not published, how long a license takes. Record each one here instead of leaving a vague "ask the agency" line.

  python3 call_sheet.py add --data DIR --priority 1 --item "Tandoor / solid-fuel approval" \\
        --office "Chicago Fire Department, Fire Prevention Bureau" --phone "312-744-6249" --email "food@cityofchicago.org" \\
        --question "Can a tandoor-style clay oven fired with charcoal or wood be approved on a licensed mobile food vehicle?" \\
        --cites "Mobile Food Vendors and Shared Kitchens Rules, Part II, Rules 2, 4 and 5" \\
        --why "Our signature product is tandoor-fired pizza"
  python3 call_sheet.py report --data DIR     markdown: numbered, most important first, one block per office
  python3 call_sheet.py check  --data DIR     fails (exit 1) if the tandoor / solid-fuel question is missing

--data defaults to reports/<slug>/data when you pass a city like "Chicago, IL". Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from typing import Dict, List, Optional

FIELDS = ["priority", "item", "office", "phone", "email", "question", "cites", "why"]
KEY_WORDS = re.compile(r"tandoor|solid[- ]fuel|open[- ]flame|charcoal|wood[- ]fired|clay oven", re.I)


def read_rows(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def data_dir(a: argparse.Namespace, ap: argparse.ArgumentParser) -> str:
    if a.data:
        return a.data
    if not a.city:
        ap.error('give a city ("Chicago, IL") or --data DIR')
    m = re.match(r"^\s*(.+?)[,\s]+([A-Za-z]{2})\s*$", a.city)
    if not m:
        ap.error('city must look like "Chicago, IL"')
    return os.path.join("reports", re.sub(r"[^a-z0-9]+", "-", f"{m.group(1)}-{m.group(2)}".lower()).strip("-"), "data")


def validate(r: Dict[str, str]) -> List[str]:
    errs = []
    if not str(r.get("priority", "")).isdigit() or int(r["priority"]) < 1:
        errs.append("priority must be a whole number from 1 (most important)")
    for k in ("item", "office", "question"):
        if not r.get(k, "").strip():
            errs.append(f"{k} is required")
    if not (r.get("phone", "").strip() or r.get("email", "").strip()):
        errs.append("give at least a phone number or an email address for the office")
    if r.get("question", "").strip() and not r["question"].strip().endswith("?"):
        errs.append("write the question as a question (end it with '?')")
    return errs


def cmd_add(path: str, a: argparse.Namespace) -> int:
    row = {"priority": str(a.priority), "item": a.item, "office": a.office, "phone": a.phone or "",
           "email": a.email or "", "question": a.question, "cites": a.cites or "", "why": a.why or ""}
    errs = validate(row)
    if errs:
        print("NOT ADDED:\n- " + "\n- ".join(errs))
        return 2
    rows = read_rows(path)
    if any(r["item"].lower() == row["item"].lower() and r["office"].lower() == row["office"].lower() for r in rows):
        print(f"Already listed: {row['item']} ({row['office']}): not added again")
        return 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    print(f"Added: {row['item']} -> {row['office']}")
    return 0


def has_key_question(rows: List[Dict[str, str]]) -> bool:
    return any(KEY_WORDS.search(r.get("item", "") + " " + r.get("question", "")) for r in rows)


def cmd_check(path: str) -> int:
    rows = read_rows(path)
    if not rows:
        print("MISSING: no questions recorded yet")
        return 1
    if not has_key_question(rows):
        print("MISSING: no question about tandoor / solid-fuel / open-flame approval. It is the one fact that can "
              "decide whether the truck concept works: add it for the fire authority.")
        return 1
    print(f"OK: {len(rows)} questions, including the tandoor / solid-fuel approval")
    return 0


def cmd_report(path: str) -> int:
    rows = sorted(read_rows(path), key=lambda r: (int(r["priority"]) if r["priority"].isdigit() else 99, r["item"]))
    if not rows:
        print("(no questions recorded: use `call_sheet.py add`)")
        return 0
    print("### Questions only an office can answer (call sheet)\n")
    print("Ask for the answer in writing (an email reply is enough) and note the name and date.\n")
    n = 0
    for office in dict.fromkeys(r["office"] for r in rows):
        mine = [r for r in rows if r["office"] == office]
        contact = "; ".join(x for x in (mine[0]["phone"], mine[0]["email"]) if x)
        print(f"**{office}** ({contact})\n")
        for r in mine:
            n += 1
            print(f"{n}. **{r['item']}** (priority {r['priority']}): {r['question']}")
            if r["cites"]:
                print(f"   - Cite: {r['cites']}")
            if r["why"]:
                print(f"   - Why it matters: {r['why']}")
        print()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("add", "report", "check"):
        p = sub.add_parser(name)
        p.add_argument("city", nargs="?")
        p.add_argument("--data")
        if name == "add":
            p.add_argument("--priority", type=int, default=5)
            p.add_argument("--item", required=True)
            p.add_argument("--office", required=True)
            p.add_argument("--phone")
            p.add_argument("--email")
            p.add_argument("--question", required=True)
            p.add_argument("--cites")
            p.add_argument("--why")
    a = ap.parse_args(argv)
    path = os.path.join(data_dir(a, ap), "questions_for_offices.csv")
    if a.cmd == "add":
        return cmd_add(path, a)
    if a.cmd == "check":
        return cmd_check(path)
    return cmd_report(path)


if __name__ == "__main__":
    sys.exit(main())
