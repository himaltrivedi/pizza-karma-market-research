#!/usr/bin/env python3
"""
data_tool.py - look inside the big CSV files in reports/<slug>/data/ without reading them whole.
Standard library only. Output is compact on purpose (cells are cut at 60 characters).

  python3 data_tool.py columns  FILE
  python3 data_tool.py head     FILE [-n 15] [--cols a,b,c]
  python3 data_tool.py filter   FILE --col X (--equals V | --contains V | --gte N | --lte N)
                                [--cols a,b,c] [-n 40] [--sort COL] [--desc]
  python3 data_tool.py count-by FILE --col X [-n 30]
  python3 data_tool.py sum-by   FILE --col GROUPCOL --sum NUMCOL [-n 30]   (rows, total and mean per group)
  python3 data_tool.py summary  FILE [--cols a,b,c]      (row count; min/median/max for numeric columns)
"""
import argparse
import csv
import statistics
import sys

CELL = 60


def load(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames or [], list(reader)


def to_num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", ""))
    except ValueError:
        return None


def pick_cols(header, spec):
    if not spec:
        return header
    want = [c.strip() for c in spec.split(",") if c.strip()]
    bad = [c for c in want if c not in header]
    if bad:
        sys.exit(f"Unknown column(s): {', '.join(bad)}. Run 'columns' to list them.")
    return want


def show(rows, cols, limit):
    def cut(v):
        v = "" if v is None else str(v).replace("\n", " ")
        return v if len(v) <= CELL else v[: CELL - 1] + "…"
    print("\t".join(cols))
    for r in rows[:limit]:
        print("\t".join(cut(r.get(c)) for c in cols))
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more rows (use -n, or narrow the filter)")


def cmd_columns(a):
    header, rows = load(a.file)
    print(f"{len(rows)} rows, {len(header)} columns")
    for c in header:
        first = next((r[c] for r in rows if r.get(c)), "")
        filled = sum(1 for r in rows if r.get(c))
        print(f"  {c}  [{filled}/{len(rows)} filled]  e.g. {str(first)[:40]}")


def cmd_head(a):
    header, rows = load(a.file)
    show(rows, pick_cols(header, a.cols), a.n)


def cmd_filter(a):
    header, rows = load(a.file)
    if a.col not in header:
        sys.exit(f"Unknown column {a.col}. Run 'columns'.")
    out = []
    for r in rows:
        v = r.get(a.col, "")
        if a.equals is not None and v.strip().lower() != a.equals.strip().lower():
            continue
        if a.contains is not None and a.contains.lower() not in v.lower():
            continue
        if a.gte is not None and not ((to_num(v) is not None) and to_num(v) >= a.gte):
            continue
        if a.lte is not None and not ((to_num(v) is not None) and to_num(v) <= a.lte):
            continue
        out.append(r)
    if a.sort:
        out.sort(key=lambda r: (to_num(r.get(a.sort)) is None, to_num(r.get(a.sort)) or 0, r.get(a.sort, "")),
                 reverse=False)
        if a.desc:
            out.reverse()
    print(f"{len(out)} of {len(rows)} rows match")
    show(out, pick_cols(header, a.cols), a.n)


def cmd_count_by(a):
    header, rows = load(a.file)
    if a.col not in header:
        sys.exit(f"Unknown column {a.col}. Run 'columns'.")
    counts = {}
    for r in rows:
        k = r.get(a.col, "") or "(blank)"
        counts[k] = counts.get(k, 0) + 1
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1])[: a.n]:
        print(f"{n}\t{k}")
    if len(counts) > a.n:
        print(f"... {len(counts) - a.n} more values")


def cmd_sum_by(a):
    header, rows = load(a.file)
    for c in (a.col, a.sum):
        if c not in header:
            sys.exit(f"Unknown column {c}. Run 'columns'.")
    groups = {}
    for r in rows:
        k = r.get(a.col, "") or "(blank)"
        n = to_num(r.get(a.sum))
        g = groups.setdefault(k, [0, 0.0])
        g[0] += 1
        if n is not None:
            g[1] += n
    print(f"{a.col}\trows\tsum_{a.sum}\tmean")
    for k, (cnt, tot) in sorted(groups.items(), key=lambda kv: -kv[1][1])[: a.n]:
        print(f"{k}\t{cnt}\t{tot:g}\t{tot / cnt:g}")
    if len(groups) > a.n:
        print(f"... {len(groups) - a.n} more groups")


def cmd_summary(a):
    header, rows = load(a.file)
    print(f"{len(rows)} rows")
    for c in pick_cols(header, a.cols):
        nums = [n for n in (to_num(r.get(c)) for r in rows) if n is not None]
        filled = sum(1 for r in rows if r.get(c))
        if filled and len(nums) >= 0.8 * filled and nums:
            print(f"{c}: min {min(nums):g}, median {statistics.median(nums):g}, max {max(nums):g}, "
                  f"sum {sum(nums):g} ({len(nums)} values)")
        else:
            print(f"{c}: {filled} filled, {len({r.get(c) for r in rows if r.get(c)})} distinct")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("columns", cmd_columns), ("head", cmd_head), ("filter", cmd_filter),
                     ("count-by", cmd_count_by), ("sum-by", cmd_sum_by), ("summary", cmd_summary)):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--col")
        p.add_argument("--cols")
        p.add_argument("--sum")
        p.add_argument("-n", type=int, default=15 if name == "head" else 40)
        if name == "filter":
            p.add_argument("--equals")
            p.add_argument("--contains")
            p.add_argument("--gte", type=float)
            p.add_argument("--lte", type=float)
            p.add_argument("--sort")
            p.add_argument("--desc", action="store_true")
        p.set_defaults(fn=fn)
    a = ap.parse_args(argv)
    if a.cmd == "sum-by" and not (a.col and a.sum):
        ap.error("--col and --sum are required")
    if a.cmd in ("filter", "count-by") and not a.col:
        ap.error("--col is required")
    if a.cmd == "filter" and a.equals is None and a.contains is None and a.gte is None and a.lte is None:
        ap.error("give one of --equals/--contains/--gte/--lte")
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
