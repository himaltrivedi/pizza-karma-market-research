#!/usr/bin/env python3
"""
build_report.py - join sections/*.md into report.md (lint input) and report.html (the deliverable), and lint it.

Usage: python3 build_report.py reports/chicago-il [--no-lint]

Lint checks (ERROR = fix before delivering, WARN = mention in Open questions):
  ERROR  placeholder text ("(source)", "TBD", "TODO", "XXX", "lorem")
  ERROR  brief missing, or required headings missing
  ERROR  brief word count outside 550-1100
  ERROR  numbers in the brief that never appear in the rest of the report (rounding and
         "2.7 million" vs "2,700,000" are accepted)
  ERROR  missing or misnamed section files (names are fixed in report-format.md)
  ERROR  a section file older than this run's start (run.json): left over from an older run
  WARN   "Today is <date>" / "Verified <date>" claims that do not match
  ERROR  Sources: two sources on one numbered line, or a number used twice in a group
  WARN   web sources without a URL; many search snippets; "estimate revisions" explanations without evidence
  WARN   Module 8 fee-table rows without a verbatim quote
  WARN   est. outside Modules 3, 4, 6 and 7, or without a "method:" line
  WARN   body paragraphs with dollar figures or percentages but no A/B/C label nearby
  WARN   dates in past years written as "upcoming"/"next"
  WARN   revenue / profit / operating-cost language
  WARN   modules with no "Not found" line
Standard library only.
"""
import datetime as dt
import glob
import html
import json
import os
import re
import sys

REQUIRED = [
    "Executive brief", "Scope", "1. Food truck competition",
    "2. Population", "3. Events", "4. Private", "5. Schools", "6. Visitors",
    "7. Sports", "8. Licensing", "9. Local truck", "Sources", "Open questions",
]
SAFE_MONEY_CONTEXT = re.compile(
    r"dep(artmen)?t\.? of revenue|revenue service|revenue agency|hotel (occupancy )?(tax )?revenue|"
    r"revenue and hotel|not a revenue estimate|\"revenue\"|revenue collection|no revenue|"
    r"revenue,? (cost|or)|(cost|profit),? or revenue|non-?profit|not-for-profit", re.I)
TREND = re.compile(r"\b(shrink\w*|declin\w*|grow(?:ing|th)|rising|rose|fell|falling|decreas\w*|increas\w*|surg\w*|"
                   r"expanding|contracting|booming)\b", re.I)
PLACEHOLDER = re.compile(r"\(source\)|\bTBD\b|\bTODO\b|\bXXX\b|lorem ipsum|\[insert", re.I)
MONEY_WORDS = re.compile(r"\b(revenue|profit|net income|operating cost|gross sales|profit margin|gross margin)\b", re.I)
LABEL = re.compile(r"\b[ABC]\)|,\s*[ABC]\b|\[[ABC]\]|\(\s*[ABC]\s*\)|\bA/B/C\b|\bgrade\s*[ABC]\b|\|\s*[ABC]\s*(?=\|)|\([ABC][:;,]|\best\.", re.I)
# A one-letter suffix (5K, $1.2M) counts only when glued to the digits: "2025, B" is an evidence label, not billions.
NUM = re.compile(r"\$?\d[\d,]*\.?\d*(?:%|\s?(?:million|billion|thousand)\b|[MBK]\b)?", re.I)
SCALES = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "b": 1e9}
CANONICAL_FILES = [
    "00-brief.md", "01-scope.md", "02-module-1-competition.md", "03-module-2-population.md",
    "04-module-3-events.md", "05-module-4-private-demand.md", "06-module-5-schools.md",
    "07-module-6-weather-visitors.md", "08-module-7-sports-nightlife.md", "09-module-8-licensing.md",
    "10-module-9-community.md", "99-sources-open-questions.md",
]


def split_sections(text):
    parts = re.split(r"(?m)^## ", text)
    out = {}
    for p in parts[1:]:
        head, _, body = p.partition("\n")
        out[head.strip()] = body
    return out


def numbers(text):
    """Numbers in text as (value, decimals, scale) tuples. '2.7 million' -> (2700000.0, 1, 1e6);
    '2,700,000' -> (2700000.0, 0, 1). Years and 1-2 digit numbers are skipped."""
    vals = []
    for m in NUM.findall(text):
        raw = m.strip()
        suffix = re.search(r"(%|million|billion|thousand|[mbk])$", raw, re.I)
        scale = 1.0
        if suffix and suffix.group(1) != "%":
            scale = SCALES[suffix.group(1).lower()]
        core = re.sub(r"[^\d.]", "", raw.replace(",", "")).rstrip(".")
        if not core or core == ".":
            continue
        try:
            x = float(core)
        except ValueError:
            continue
        if len(core.replace(".", "")) < 3 and scale == 1.0:      # skip 1-2 digit numbers
            continue
        if re.fullmatch(r"(19|20)\d\d", core):                  # skip years
            continue
        dec = len(core.split(".")[1]) if "." in core else 0
        vals.append((x * scale, dec, scale, raw.rstrip(",. ")))
    return vals


def number_in(target, pool):
    """True if the brief number appears in the pool, allowing rounding and reformatting
    (12% vs 12.4%, 2.7 million vs 2,700,000, $1,000 vs 1000) or a difference under 1%."""
    v, dec, scale = target[:3]
    for b, *_ in pool:
        if b == v or abs(b - v) <= 0.01 * max(abs(b), abs(v)):
            return True
        if round(b / scale, dec) == round(v / scale, dec):
            return True
    return False


def check_section_files(files):
    names = [os.path.basename(f) for f in files]
    errs = [f"Missing section file {c} (see report-format.md)" for c in CANONICAL_FILES if c not in names]
    errs += [f"Unexpected section file {n}: use the exact names in report-format.md" for n in names
             if n not in CANONICAL_FILES]
    return errs


def lint(report, sections_text):
    errors, warns = [], []
    heads = list(split_sections(report).keys())
    for req in REQUIRED:
        if not any(h.startswith(req) for h in heads):
            errors.append(f"Missing section heading starting '{req}'")
    for m in PLACEHOLDER.finditer(report):
        line = report[: m.start()].count("\n") + 1
        errors.append(f"Placeholder text '{m.group(0)}' at report line {line}")

    secs = split_sections(report)
    brief_key = next((h for h in secs if h.startswith("Executive brief")), None)
    if brief_key:
        brief = secs[brief_key]
        words = len(re.findall(r"\S+", brief))
        if not 550 <= words <= 1100:
            errors.append(f"Brief is {words} words (target about 800, allowed 550-1100)")
        body = "\n".join(v for k, v in secs.items() if k != brief_key)
        body_nums = numbers(body)
        missing = sorted({raw for v, d, sc, raw in numbers(brief) if not number_in((v, d, sc), body_nums)})
        if missing:
            errors.append("Numbers in the brief not found in the body (rounding allowed): " + ", ".join(missing[:25]))
        if not re.search(r"confidence", brief, re.I):
            errors.append("Brief has no confidence table")
        if not re.search(r"next data pulls", brief, re.I):
            errors.append("Brief has no 'Next data pulls' list")
        for line in brief.splitlines():
            if TREND.search(line) and len(set(re.findall(r"\b(?:19|20)\d\d\b", line))) < 2:
                warns.append("Trend claim without two dated figures in the same line: '" + line.strip()[:90] +
                             "...' (give both years and numbers, or drop the trend word)")
        if not re.search(r"tandoor|solid[- ]fuel|open[- ]flame", brief, re.I):
            warns.append("The brief does not mention the tandoor / solid-fuel approval question: it is the one fact "
                         "that can decide whether the truck concept works")

    year = dt.date.today().year
    for m in re.finditer(r"(?i)\b(upcoming|next|this coming)\b[^.\n]{0,60}\b(20\d\d)\b", report):
        if int(m.group(2)) < year:
            warns.append(f"Past year {m.group(2)} used with '{m.group(1)}': '{m.group(0)[:80]}'")

    for m in MONEY_WORDS.finditer(report):
        ctx = report[max(0, m.start() - 40): m.end() + 40].replace("\n", " ")
        if SAFE_MONEY_CONTEXT.search(ctx):
            continue
        warns.append(f"Revenue/profit/cost language: '...{ctx}...' (allowed only if stating the rule)")

    for m in re.finditer(r"reflects?\s+(?:the\s+)?(?:ACS\s+)?estimate revisions?", report, re.I):
        warns.append("A difference is explained as 'estimate revisions' with no evidence: cite the 2020 census count "
                     "for that place (change_vs_2020_pct / pop_change_note) or drop the explanation")
        break
    for h, body in secs.items():
        if re.search(r"\best\.", body):
            if h.startswith("8.") or h.startswith("8 ") or h.startswith(("1.", "2.", "5.", "9.")):
                warns.append(f"'{h}' uses est.: estimates belong in Modules 3, 4, 6 and 7 only")
            elif re.match(r"\d\.", h) and not re.search(r"method:", body, re.I):
                warns.append(f"'{h}' uses est. but has no 'method:' line")
        if h.startswith("8.") or h.startswith("8 "):
            for row in body.splitlines():
                if (row.lstrip().startswith("|") and re.search(r"\$\s?\d", row) and not re.search(r"[\"\u201c]", row)
                        and not re.search(r"(?i)subtotal|\btotal\b", row)):
                    warns.append(f"Module 8 fee row has no verbatim quote: '{row.strip()[:70]}...'")
        if re.match(r"\d\.", h) and not re.search(r"(?i)not found", body):
            warns.append(f"Module '{h}' has no 'Not found' line")
        if re.match(r"\d\.", h):
            paras = [
                "\n".join(l for l in para.splitlines() if not l.lstrip().startswith("#"))
                for para in re.split(r"\n\s*\n", body)]
            for i, para in enumerate(paras):
                if not re.search(r"\$\d|\d+\s?%", para) or LABEL.search(para):
                    continue
                # a source line just above or below the table/paragraph also counts
                near = " ".join(paras[max(0, i - 1): i] + paras[i + 1: i + 2])
                if LABEL.search(near) or re.search(r"\.csv\b", near):
                    continue
                snippet = para.strip().splitlines()[0][:70] if para.strip() else ""
                warns.append(f"No A/B/C label near figures in '{h}': '{snippet}...'")
                break
    return errors, warns


DATE_CLAIM = re.compile(r"\b(Today is|Verified)\s+(\d{4}-\d{2}-\d{2})")


def check_freshness(folder, files):
    """Sections must come from THIS run: compare file dates with reports/<slug>/run.json and look for date
    claims ("Today is ...", "Verified ...") that do not match."""
    errors, warns = [], []
    started = None
    try:
        with open(os.path.join(folder, "run.json"), encoding="utf-8") as fh:
            started = dt.date.fromisoformat(json.load(fh).get("started", ""))
    except (OSError, ValueError):
        warns.append("No run.json in the report folder: cannot tell whether every section was written in this run "
                     "(pull_data.py creates it)")
    for f in files:
        name = os.path.basename(f)
        written = dt.date.fromtimestamp(os.path.getmtime(f))
        if started and written < started:
            errors.append(f"{name} was last written {written}, before this run started ({started}): it is left over "
                          "from an older run. Redo it (or move it to sections_archive/)")
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        for m in DATE_CLAIM.finditer(text):
            claimed = dt.date.fromisoformat(m.group(2))
            if m.group(1) == "Today is" and abs((claimed - written).days) > 1:
                warns.append(f"{name} says 'Today is {claimed}' but was written {written}: update or remove the date")
            elif m.group(1) == "Verified" and started and claimed < started:
                warns.append(f"{name} says 'Verified {claimed}', before this run started ({started}): verify again")
    return errors, warns


URL_LIKE = re.compile(r"https?://|\b[\w-]+(?:\.[\w-]+)*\.(?:gov|com|org|edu|net|us|io|info)\b", re.I)
TWO_ON_A_LINE = re.compile(r"[.;)]\s+\d{1,2}\.\s+[A-Z]")


def check_sources(report):
    """Sources hygiene: one entry per line, a URL (or a domain) on every web source, not mostly snippets."""
    errors, warns = [], []
    secs = split_sections(report)
    key = next((h for h in secs if h.startswith("Sources")), None)
    if not key:
        return errors, warns
    group = ""
    nums = {}
    for line in secs[key].splitlines():
        if line.startswith("###"):
            group = line.lstrip("# ").strip().lower()
            nums = {}
            continue
        m = re.match(r"^\s*(\d{1,3})\.\s+(.*)$", line)
        if not m:
            continue
        n, rest = int(m.group(1)), m.group(2)
        if TWO_ON_A_LINE.search(rest):
            errors.append(f"Sources line {n} holds more than one source (\"...{TWO_ON_A_LINE.search(rest).group(0)}...\"): "
                          "put each source on its own numbered line")
        if n in nums:
            errors.append(f"Sources number {n} is used twice in '{group or 'Sources'}'")
        nums[n] = True
        if group.startswith("web") and not URL_LIKE.search(rest) and not re.search(r"other .*sources", rest, re.I):
            warns.append(f"Web source {n} has no URL or domain: '{rest[:60]}...'")
    snippets = len(re.findall(r"search (?:snippet|summary)|snippet", report, re.I))
    if snippets > 10:
        warns.append(f"{snippets} mentions of search snippets/summaries: a report resting on snippets is weak; open "
                     "the page behind the important ones or say plainly which facts are snippet-level")
    return errors, warns


def _inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    # bare URLs (not already inside a link): link them, keeping trailing punctuation outside
    s = re.sub(r'(?<!["\'>=])\b(https?://[^\s<]*[^\s<).,;:!?\'"])', r'<a href="\1">\1</a>', s)
    return s


LIST_ITEM = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")


def render_list(lines):
    """Render consecutive list lines (with indentation) as nested <ul>/<ol>. When the top-level list type changes
    (numbered, then bullets) the block is split into separate lists instead of merging them."""
    parsed = [LIST_ITEM.match(ln.expandtabs(4)) for ln in lines]
    top = min(len(m.group(1)) for m in parsed)
    groups, last = [], None
    for ln, m in zip(lines, parsed):
        kind = "ol" if m.group(2)[0].isdigit() else "ul"
        if len(m.group(1)) == top and last is not None and kind != last:
            groups.append([])
        if not groups:
            groups.append([])
        if len(m.group(1)) == top:
            last = kind
        groups[-1].append(ln)
    if len(groups) > 1:
        return "".join(render_list(g) for g in groups)
    items = []
    for ln in lines:
        m = LIST_ITEM.match(ln.expandtabs(4))
        items.append((len(m.group(1)), "ol" if m.group(2)[0].isdigit() else "ul", m.group(3)))

    def build(i, indent):
        tag = items[i][1]
        out = [f"<{tag}>"]
        while i < len(items) and items[i][0] >= indent:
            ind, _, text = items[i]
            if ind > indent:                      # deeper item without a parent: treat as nested in the last li
                sub, i = build(i, ind)
                out[-1] = out[-1].replace("</li>", sub + "</li>") if out[-1].endswith("</li>") else out[-1] + sub
                continue
            i += 1
            li = f"<li>{_inline(text)}"
            if i < len(items) and items[i][0] > ind:
                sub, i = build(i, items[i][0])
                li += sub
            out.append(li + "</li>")
        out.append(f"</{tag}>")
        return "".join(out), i
    html_, _ = build(0, items[0][0])
    return html_


PILLS = {"high": "g", "best": "g", "good": "g", "medium": "a", "fair": "a", "slow": "a", "low": "r", "poor": "r",
         "worst": "r"}
NUMERIC_CELL = re.compile(r"^[~$±\d.,%+\-–/ ]*\d[~$±\d.,%+\-–/ ]*$")


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>", "", text).lower()).strip("-") or "section"


def _badges(s):
    """Evidence labels written as ', A)' / '(C)' / ', est.)' become colored badges; 'Not found' is highlighted."""
    s = re.sub(r"([(,]\s?)([ABC])(\))", r'\1<span class="ev ev-\2" title="Evidence label \2">\2</span>\3', s)
    s = re.sub(r"([(,]\s?)(est\.)(\))", r'\1<span class="ev ev-est" title="Modeled estimate">est.</span>\3', s)
    return re.sub(r"\bNot found\b", '<mark class="nf">Not found</mark>', s)


def _cell(c):
    plain = c.strip()
    word = plain.lower()
    if word in PILLS:
        return f"<td><span class='pill p-{PILLS[word]}'>{html.escape(plain)}</span></td>"
    cls = " class='num'" if NUMERIC_CELL.match(plain) else ""
    return f"<td{cls}>{_badges(_inline(c))}</td>"


REPORT_CSS = """
:root{--bg:#f4f1ec;--card:#fff;--ink:#211d1a;--muted:#6b645d;--line:#e3ddd5;--accent:#b4492d;--accent-soft:#f8e9e3;
--zebra:#faf8f5;--head:#efe9e1;--g:#1f7a4d;--gbg:#e3f3ea;--a:#8a5a00;--abg:#fbefd0;--r:#a3312a;--rbg:#f9e0dd;
--link:#9a3a20;--shadow:0 1px 2px rgba(40,25,10,.06),0 6px 20px rgba(40,25,10,.05)}
@media(prefers-color-scheme:dark){:root{--bg:#141210;--card:#1d1a17;--ink:#ece6df;--muted:#a1988e;--line:#33302b;
--accent:#e8856a;--accent-soft:#2c1f1a;--zebra:#211e1b;--head:#2a2622;--g:#6fd3a0;--gbg:#17301f;--a:#f0c36a;--abg:#33290f;
--r:#f19a93;--rbg:#3a1e1b;--link:#f0a58f;--shadow:none}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,
Roboto,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--link);text-underline-offset:2px}
.hero{background:linear-gradient(135deg,#7d2d19,#b4492d 55%,#d98a4a);color:#fff;padding:42px 16px 34px}
.hero-in{max-width:1180px;margin:0 auto}
.hero h1{margin:0 0 .35em;font-size:clamp(1.7rem,4vw,2.6rem);line-height:1.15;letter-spacing:-.01em}
.hero p{margin:.2em 0;max-width:62ch;opacity:.95;font-size:1.02rem}
.layout{display:grid;grid-template-columns:250px minmax(0,1fr);gap:28px;max-width:1180px;margin:0 auto;padding:24px 16px 64px}
.toc{position:sticky;top:16px;align-self:start;max-height:calc(100vh - 32px);overflow:auto;font-size:.88rem;
background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 10px;box-shadow:var(--shadow)}
.toc b{display:block;padding:0 10px 8px;color:var(--muted);font-size:.72rem;letter-spacing:.09em;text-transform:uppercase}
.toc a{display:block;padding:6px 10px;border-radius:8px;color:var(--ink);text-decoration:none;line-height:1.3}
.toc a:hover{background:var(--accent-soft)}.toc a.on{background:var(--accent-soft);color:var(--accent);font-weight:600}
main{min-width:0}
section{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:26px 30px;margin:0 0 22px;
box-shadow:var(--shadow);scroll-margin-top:16px}
section.brief{border-top:5px solid var(--accent)}
h2{margin:0 0 .6em;font-size:1.55rem;line-height:1.25;letter-spacing:-.01em}
h3{margin:1.7em 0 .5em;font-size:.82rem;letter-spacing:.09em;text-transform:uppercase;color:var(--accent)}
h4{margin:1.3em 0 .4em;font-size:1rem}
p,li{max-width:76ch;overflow-wrap:anywhere}p{margin:.7em 0}ul,ol{padding-left:1.3em;margin:.6em 0}li{margin:.3em 0}li>ul,li>ol{margin:.2em 0}
p.finding{font-size:1.1rem;line-height:1.55;padding:12px 16px;margin:0 0 1.1em;background:var(--accent-soft);
border-left:4px solid var(--accent);border-radius:0 10px 10px 0;max-width:none}
p.lead{font-size:1.02rem;color:var(--muted)}
.callout{margin:1.2em 0 .2em;padding:12px 16px;background:var(--abg);color:var(--ink);border-radius:10px;
border-left:4px solid var(--a);font-size:.94rem;max-width:none}
.tw{overflow-x:auto;margin:1em 0;border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:.9rem;line-height:1.45}
th{background:var(--head);text-align:left;font-weight:600;font-size:.78rem;letter-spacing:.03em;color:var(--muted);
text-transform:uppercase;white-space:nowrap}
th,td{padding:9px 13px;vertical-align:top;border-bottom:1px solid var(--line)}
tbody tr:nth-child(even){background:var(--zebra)}tbody tr:last-child td{border-bottom:0}
td:first-child{font-weight:600}td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.ev{display:inline-block;min-width:1.3em;padding:0 .3em;margin:0 .05em;border-radius:5px;font-size:.74em;font-weight:700;
text-align:center;line-height:1.5;vertical-align:.08em;font-style:normal}
.ev-A{background:var(--gbg);color:var(--g)}.ev-B{background:var(--abg);color:var(--a)}
.ev-C{background:var(--rbg);color:var(--r)}.ev-est{background:var(--head);color:var(--muted)}
mark.nf{background:var(--abg);color:var(--a);padding:0 .25em;margin-right:-.2em;border-radius:4px;font-weight:600}
.pill{display:inline-block;padding:1px 10px;border-radius:99px;font-size:.8rem;font-weight:600}
.p-g{background:var(--gbg);color:var(--g)}.p-a{background:var(--abg);color:var(--a)}.p-r{background:var(--rbg);color:var(--r)}
code{background:var(--head);padding:.05em .35em;border-radius:5px;font-size:.88em}
blockquote{margin:1em 0;padding:.3em 1em;border-left:4px solid var(--line);color:var(--muted)}
hr{border:0;border-top:1px solid var(--line);margin:1.6em 0}
section#sources ol,section.sources ol{font-size:.86rem;color:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:.82rem;color:var(--muted);margin:0 0 14px}
@media(max-width:900px){.layout{grid-template-columns:minmax(0,1fr)}.toc{position:static;max-height:none}
section{padding:20px 16px;border-radius:12px}.hero{padding:30px 16px 24px}}
@media print{body{background:#fff;font-size:11pt}.toc,.legend{display:none}.layout{display:block;padding:0}
.hero{background:none;color:#000;padding:0 0 12px}section{box-shadow:none;border:0;padding:0 0 12px;break-inside:auto}
.tw{break-inside:avoid;border:1px solid #bbb}h2{break-after:avoid}}
"""

REPORT_JS = ("(function(){try{var l=[].slice.call(document.querySelectorAll('.toc a'));if(!l.length||!window.IntersectionObserver)return;"
             "var o=new IntersectionObserver(function(e){e.forEach(function(x){if(x.isIntersecting){l.forEach(function(a){"
             "a.classList.toggle('on',a.getAttribute('href')==='#'+x.target.id)})}})},{rootMargin:'-10% 0px -80% 0px'});"
             "document.querySelectorAll('main section').forEach(function(s){o.observe(s)})}catch(e){}})();")


def md_to_html(md, title):
    """Small markdown subset -> a styled report page: hero, sticky contents, one card per H2 section."""
    out, i, lines = [], 0, md.split("\n")
    h1, lead, toc = None, [], []
    in_section, finding_next = False, False

    def emit(piece):
        out.append(piece)

    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
        elif ln.startswith("#"):
            n = len(ln) - len(ln.lstrip("#"))
            text = ln[n:].strip()
            if n == 1 and h1 is None:
                h1 = _inline(text)
            elif n == 2:
                if in_section:
                    emit("</section>")
                sid = _slug(text)
                toc.append((sid, _inline(text)))
                cls = []
                if "executive brief" in text.lower():
                    cls.append("brief")
                if text.lower().startswith(("sources", "open questions", "verification")):
                    cls.append("sources")
                emit(f"<section id='{sid}'" + (f" class='{' '.join(cls)}'" if cls else "") + ">")
                emit(f"<h2>{_inline(text)}</h2>")
                in_section, finding_next = True, True
            else:
                emit(f"<h{n}>{_inline(text)}</h{n}>")
                finding_next = False
            i += 1
        elif ln.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            rows = [r for r in rows if not all(re.fullmatch(r":?-+:?", c) for c in r)]
            h = "".join(f"<th>{_inline(c)}</th>" for c in rows[0])
            b = "".join("<tr>" + "".join(_cell(c) for c in r) + "</tr>" for r in rows[1:])
            emit(f"<div class='tw'><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>")
            finding_next = False
        elif re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", ln):
            emit("<hr>")
            i += 1
        elif ln.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i].lstrip("> ").strip())
                i += 1
            emit(f"<blockquote>{_inline(' '.join(quote))}</blockquote>")
            finding_next = False
        elif LIST_ITEM.match(ln):
            block = []
            while i < len(lines) and LIST_ITEM.match(lines[i]):
                block.append(lines[i])
                i += 1
            emit(render_list(block).replace("<li>", "<li>", 1))
            finding_next = False
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith("#") \
                    and not lines[i].lstrip().startswith("|") \
                    and not LIST_ITEM.match(lines[i]) and not lines[i].startswith(">"):
                para.append(lines[i].strip())
                i += 1
            raw = " ".join(para)
            body = _badges(_inline(raw))
            if not in_section:
                lead.append(f"<p>{body}</p>")
            elif raw.startswith("Not found"):
                emit(f"<p class='callout'>{body}</p>")
                finding_next = False
            elif finding_next:
                emit(f"<p class='finding'>{body}</p>")
                finding_next = False
            else:
                emit(f"<p>{body}</p>")
    if in_section:
        emit("</section>")
    page_title = h1 or html.escape(title)
    hero = f"<header class='hero'><div class='hero-in'><h1>{page_title}</h1>{''.join(lead)}</div></header>"
    nav = ""
    if toc:
        nav = ("<nav class='toc' aria-label='Contents'><b>Contents</b>"
               + "".join(f"<a href='#{sid}'>{t}</a>" for sid, t in toc) + "</nav>")
    legend = ("<div class='legend'><span><span class='ev ev-A'>A</span> opened official source or data file</span>"
              "<span><span class='ev ev-B'>B</span> opened secondary page</span>"
              "<span><span class='ev ev-C'>C</span> search summary or unopened page</span>"
              "<span><span class='ev ev-est'>est.</span> modeled estimate</span></div>") if toc else ""
    body = "\n".join(out)
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{REPORT_CSS}</style></head><body>\n"
            f"{hero}\n<div class='layout'>{nav}<main>{legend}\n{body}\n</main></div>\n"
            f"<script>{REPORT_JS}</script>\n</body></html>\n")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    folder = args[0]
    files = sorted(glob.glob(os.path.join(folder, "sections", "*.md")))
    if not files:
        full = os.path.abspath(folder)
        why = ("the folder does not exist" if not os.path.isdir(folder) else
               "the folder has no sections/ subfolder" if not os.path.isdir(os.path.join(folder, "sections"))
               else "sections/ has no .md files")
        print(f"ERROR: no section files in {full}/sections/ ({why}). Run from the folder that holds reports/, "
              "or pass the full path.")
        return 2
    file_errors = [] if "--no-lint" in sys.argv else check_section_files(files)
    chunks = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            chunks.append(fh.read().strip())
    report = "\n\n".join(chunks) + "\n"
    out = os.path.join(folder, "report.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(report)
    m = re.match(r"#\s+(.+)", report)
    html_out = os.path.join(folder, "report.html")
    with open(html_out, "w", encoding="utf-8") as fh:
        fh.write(md_to_html(report, m.group(1) if m else "Market report"))
    print(f"Wrote {html_out}")
    print(f"Wrote {out} from {len(files)} section files "
          f"({len(report.split())} words)")
    if "--no-lint" in sys.argv:
        return 0
    errors, warns = lint(report, chunks)
    fe, fw = check_freshness(folder, files)
    se, sw = check_sources(report)
    errors = file_errors + fe + se + errors
    warns = fw + sw + warns
    for e in errors:
        print("ERROR:", e)
    for w in warns:
        print("WARN: ", w)
    print(f"\n{len(errors)} errors, {len(warns)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
