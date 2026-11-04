"""Tests for scripts/build_report.py using seeded section folders."""
import contextlib
import datetime as dt
import glob
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import build_report as br  # noqa: E402

BRIEF_BODY = (
    "### Market snapshot\n"
    "- Metro population is about 2.7 million (A). Median household income $72,400 (A). About 12% are 18-34 (A).\n"
    "- 450 licensed trucks (B).\n\n"
    "### Competition\n- Pizza trucks are few (C).\n\n"
    "### Where and when\n- May to September are strongest, low confidence.\n\n"
    "### Licensing and first-year fees\n- Listed fees total $1,580 (A) and exclude plan review.\n\n"
    "### Biggest unknowns\n- Whether a tandoor is approved on a truck.\n\n"
    "| Module | Confidence | Why |\n| --- | --- | --- |\n| 1 | Medium | counts from directory |\n\n"
    "**Next data pulls:** county clerk marriage counts; employer list.\n\n"
)
FILLER = "Truck operators plan around the season and the events calendar every year. " * 1


def module(heading, extra=""):
    return f"## {heading}\n\n- Item with figure 2,700,000 people (A).\n{extra}\n- Not found: x. Where to get it: y.\n"


def make_sections(d, brief_extra_words=700, brief_numbers=BRIEF_BODY, body_extra="", skip=(), extra_files=()):
    sec = os.path.join(d, "sections")
    os.makedirs(sec)
    brief = "# Test, TS Market Report: Pizza Karma\n\n## Executive brief\n\n" + brief_numbers + \
        ("filler " * brief_extra_words) + "\n"
    files = {
        "00-brief.md": brief,
        "01-scope.md": "## Scope\n\nMetro scope.\n",
        "02-module-1-competition.md": module("1. Food truck competition", "- 450 trucks (B).\n"),
        "03-module-2-population.md": module("2. Population, neighborhoods and liveliness",
                                            "- Income $72,412 (A). Share 12.4% age 18-34 (A).\n" + body_extra),
        "04-module-3-events.md": module("3. Events and seasons"),
        "05-module-4-private-demand.md": module("4. Private and catering demand"),
        "06-module-5-schools.md": module("5. Schools and campuses"),
        "07-module-6-weather-visitors.md": module("6. Visitors, weather and operating season"),
        "08-module-7-sports-nightlife.md": module("7. Sports, nightlife and lunch clusters"),
        "09-module-8-licensing.md": module("8. Licensing, permits and operating rules",
                                           '| Item | Fee | Term | Quote |\n| --- | --- | --- | --- |\n'
                                           '| License | $1,000 | one-time | "The fee is $1,000" |\n'
                                           '| Subtotal | $1,580 | | |\n'),
        "10-module-9-community.md": module("9. Local truck community and marketing"),
        "99-sources-open-questions.md": "## Sources\n\n1. Census (A).\n\n## Open questions\n\n- none\n\n## Verification log\n\n| a | b |\n",
    }
    for k in skip:
        files.pop(k)
    for name in extra_files:
        files[name] = "## Extra\n\ntext\n"
    for name, text in files.items():
        with open(os.path.join(sec, name), "w", encoding="utf-8") as f:
            f.write(text)
    return d


def build(**kw):
    d = tempfile.mkdtemp()
    try:
        make_sections(d, **kw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            old = sys.argv
            sys.argv = ["build_report.py", d]
            try:
                rc = br.main()
            finally:
                sys.argv = old
        out = buf.getvalue()
        report_exists = os.path.exists(os.path.join(d, "report.html"))
        return rc, out, report_exists
    finally:
        shutil.rmtree(d, ignore_errors=True)


class Numbers(unittest.TestCase):
    def test_million_matches_full_number(self):
        pool = br.numbers("population 2,700,000 people")
        self.assertTrue(br.number_in(br.numbers("2.7 million")[0], pool))

    def test_percent_rounding(self):
        pool = br.numbers("share was 12.4% of residents")
        self.assertEqual(br.numbers("12%"), [])          # 1-2 digit numbers are ignored by design
        self.assertFalse(br.number_in(br.numbers("12.0%")[0], pool))
        self.assertTrue(br.number_in(br.numbers("12.4%")[0], pool))
        self.assertFalse(br.number_in(br.numbers("18.5%")[0], pool))

    def test_dollars_rounded(self):
        pool = br.numbers("income $72,412")
        self.assertTrue(br.number_in(br.numbers("$72,400")[0], pool))
        self.assertFalse(br.number_in(br.numbers("$90,000")[0], pool))

    def test_years_skipped(self):
        self.assertEqual(br.numbers("in 2026 and 1999"), [])


class Lint(unittest.TestCase):
    def test_good_report_passes_even_with_rounded_numbers(self):
        rc, out, exists = build()
        self.assertEqual(rc, 0, out)
        self.assertTrue(exists)
        self.assertIn("0 errors", out)

    def test_number_missing_from_body_fails(self):
        rc, out, _ = build(brief_numbers=BRIEF_BODY + "- Population of 987,654 lives downtown (A).\n")
        self.assertEqual(rc, 1)
        self.assertIn("987,654", out)

    def test_brief_too_short(self):
        rc, out, _ = build(brief_extra_words=0, brief_numbers="### Market snapshot\n- x\n")
        self.assertEqual(rc, 1)
        self.assertIn("Brief is", out)

    def test_brief_too_long(self):
        rc, out, _ = build(brief_extra_words=1500)
        self.assertEqual(rc, 1)
        self.assertIn("Brief is", out)

    def test_placeholder(self):
        rc, out, _ = build(body_extra="- Fee is (source).\n- TBD later\n")
        self.assertEqual(rc, 1)
        self.assertIn("Placeholder", out)

    def test_missing_confidence_table_and_next_pulls(self):
        rc, out, _ = build(brief_numbers="### Market snapshot\n- nothing\n")
        self.assertEqual(rc, 1)
        self.assertIn("confidence table", out)
        self.assertIn("Next data pulls", out)

    def test_missing_section_file(self):
        rc, out, _ = build(skip=("05-module-4-private-demand.md",))
        self.assertEqual(rc, 1)
        self.assertIn("Missing section file 05-module-4-private-demand.md", out)

    def test_misnamed_section_file(self):
        rc, out, _ = build(extra_files=("11-bonus.md",))
        self.assertEqual(rc, 1)
        self.assertIn("Unexpected section file 11-bonus.md", out)

    def test_past_year_upcoming_warns(self):
        rc, out, _ = build(body_extra="- The upcoming festival in 2019 is big (B).\n")
        self.assertIn("Past year 2019", out)

    def test_fee_row_without_quote_warns_but_subtotal_does_not(self):
        rc, out, _ = build(body_extra="")
        self.assertNotIn("fee row has no verbatim quote", out)

    def test_department_of_revenue_not_flagged(self):
        rc, out, _ = build(body_extra="- Illinois Department of Revenue registers sales tax (A).\n")
        self.assertNotIn("Revenue/profit", out)

    def test_margin_of_error_and_revenue_collection_not_flagged(self):
        rc, out, _ = build(body_extra="- Income $72,400 (margin of error $454) (A).\n- Aurora Revenue Collection office (B).\n- Sources carry no revenue claim.\n")
        self.assertNotIn("Revenue/profit", out)

    def test_table_label_column_counts(self):
        extra = "\n### Fee table\n\n| Item | Amount | Label |\n| --- | --- | --- |\n| Permit | $150 | A |\n"
        rc, out, _ = build(body_extra=extra)
        self.assertNotIn("No A/B/C label", out)

    def test_real_revenue_language_warns(self):
        rc, out, _ = build(body_extra="- Expect revenue of strong levels (C).\n")
        self.assertIn("Revenue/profit", out)


if __name__ == "__main__":
    unittest.main()


class HtmlRender(unittest.TestCase):
    SAMPLE = ("# Chicago, IL Market Report: Pizza Karma\n\nResearch date 2026-11-03.\n\n## Executive brief\n\n"
              "Chicago is large (9,371,595 people, ACS, A).\n\n- Fee about 100 dollars (Source, 2026, C), model (est.)\n\n"
              "| Module | Confidence | Count |\n| --- | --- | --- |\n| 1 Competition | Medium | 1,234 |\n"
              "| 8 Licensing | Low | $150 |\n\n## 2. Population\n\nFinding sentence.\n\nNot found: hotel data.\n")

    def test_page_has_hero_contents_and_one_card_per_section(self):
        h = br.md_to_html(self.SAMPLE, "t")
        self.assertIn("<header class='hero'>", h)
        self.assertIn("<h1>Chicago, IL Market Report: Pizza Karma</h1>", h)
        self.assertIn("<nav class='toc'", h)
        self.assertEqual(h.count("<section "), 2)
        self.assertIn("class='brief'", h)
        self.assertIn("href='#executive-brief'", h)
        self.assertIn("href='#2-population'", h)
        self.assertEqual(h.count("</section>"), 2)

    def test_evidence_labels_become_badges_and_not_found_is_highlighted(self):
        h = br.md_to_html(self.SAMPLE, "t")
        self.assertIn("<span class=\"ev ev-A\"", h.replace("'", '"'))
        self.assertIn("ev-C", h)
        self.assertIn("ev-est", h)
        self.assertIn('<mark class="nf">Not found</mark>', h)
        self.assertIn("<p class='callout'>", h)

    def test_first_paragraph_after_a_heading_is_the_finding(self):
        h = br.md_to_html(self.SAMPLE, "t")
        self.assertEqual(h.count("class='finding'"), 2)

    def test_tables_get_rating_pills_and_numeric_alignment(self):
        h = br.md_to_html(self.SAMPLE, "t")
        self.assertIn("pill p-a", h)           # Medium
        self.assertIn("pill p-r", h)           # Low
        self.assertIn("<td class='num'>1,234</td>", h)
        self.assertIn("<td class='num'>$150</td>", h)

    def test_plain_a_b_c_text_is_not_turned_into_badges(self):
        h = br.md_to_html("Choose option (A, B or C) and the letter B alone.", "t")
        self.assertNotIn('<span class="ev', h)

    def test_numbered_then_bullet_lists_are_not_merged(self):
        h = br.md_to_html("## S\n\n1. one\n2. two\n- bullet a\n- bullet b\n", "t")
        self.assertIn("<ol><li>one</li><li>two</li></ol><ul><li>bullet a</li><li>bullet b</li></ul>", h)

    def test_page_without_h2_still_renders(self):
        h = br.md_to_html("Just a paragraph.", "t")
        self.assertIn("<p>Just a paragraph.</p>", h)
        self.assertNotIn("<nav", h)

    def test_nested_lists(self):
        h = br.md_to_html("- one\n  - child a\n  - child b\n    - grandchild\n- two\n", "t")
        self.assertIn("<li>one<ul><li>child a</li><li>child b<ul><li>grandchild</li></ul></li></ul></li>", h)
        self.assertIn("<li>two</li>", h)

    def test_numbered_with_bullets_inside(self):
        h = br.md_to_html("1. first\n   - note\n2. second\n", "t")
        self.assertIn("<ol><li>first<ul><li>note</li></ul></li><li>second</li></ol>", h)

    def test_bare_urls_linked_but_markdown_links_not_doubled(self):
        h = br.md_to_html("See https://example.gov/a/b. And [page](https://x.org/p) too (https://y.org/q).", "t")
        self.assertIn('<a href="https://example.gov/a/b">https://example.gov/a/b</a>.', h)
        self.assertEqual(h.count('href="https://x.org/p"'), 1)
        self.assertNotIn('href="https://x.org/p"><a', h)
        self.assertIn('<a href="https://y.org/q">https://y.org/q</a>)', h)

    def test_quote_hr_table(self):
        h = br.md_to_html("> quoted text\n\n---\n\n| a | b |\n| --- | --- |\n| 1 | **2** |\n", "t")
        self.assertIn("<blockquote>quoted text</blockquote>", h)
        self.assertIn("<hr>", h)
        self.assertIn("<td><strong>2</strong></td>", h)

    def test_real_report_renders_nested(self):
        sec = os.path.join(HERE, "fixtures", "nested_section.md")
        with open(sec, encoding="utf-8") as f:
            h = br.md_to_html(f.read(), "t")
        self.assertGreater(h.count("<ul>"), 1)
        self.assertNotIn("<li>- ", h)


class NumberEdgeCases(unittest.TestCase):
    def test_evidence_label_after_comma_is_not_billions(self):
        for text in ("(about 969,000 in 2025, B)", "(more than 20,000, B)", "about 5,400, C and 12,300, A"):
            vals = [v for v, *_ in br.numbers(text)]
            self.assertTrue(all(v < 1e7 for v in vals), (text, vals))

    def test_glued_suffix_and_words_still_scale(self):
        self.assertEqual(br.numbers("$1.2M")[0][0], 1.2e6)
        self.assertEqual(br.numbers("5K")[0][0], 5000)
        self.assertEqual(br.numbers("2.7 million")[0][0], 2.7e6)
        self.assertEqual(br.numbers("3 billion")[0][0], 3e9)

    def test_error_names_the_offending_number(self):
        rc, out, _ = build(brief_numbers=BRIEF_BODY + "- About 987,654 people (B).\n")
        self.assertEqual(rc, 1)
        self.assertIn("987,654", out)
        self.assertNotIn("e+", out)

    def test_missing_folder_message(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            old = sys.argv
            sys.argv = ["build_report.py", "reports/definitely-not-here"]
            try:
                rc = br.main()
            finally:
                sys.argv = old
        self.assertEqual(rc, 2)
        self.assertIn("does not exist", buf.getvalue())
        self.assertIn(os.getcwd(), buf.getvalue())


class LabelNeighbours(unittest.TestCase):
    def test_source_line_above_table_counts(self):
        extra = "\n(acs_places_core.csv, ACS 2024, A)\n\n| Place | Share |\n| --- | --- |\n| X | 12.4% |\n"
        rc, out, _ = build(body_extra=extra)
        self.assertNotIn("No A/B/C label", out)

    def test_figure_with_no_label_anywhere_still_warns(self):
        report = ("## 2. Population\n\nIntro sentence with no figures.\n\nShare is 12.4% overall.\n\n"
                  "Another sentence without figures.\n\nNot found: x.\n")
        _, warns = br.lint(report, [])
        self.assertTrue(any("No A/B/C label" in w and "2. Population" in w for w in warns), warns)

    def test_label_in_neighbouring_paragraph_silences(self):
        report = ("## 2. Population\n\nShare is 12.4% overall.\n\n(acs_places_core.csv, 2024, A)\n\nNot found: x.\n")
        _, warns = br.lint(report, [])
        self.assertFalse(any("No A/B/C label" in w for w in warns), warns)


class MoreFalsePositives(unittest.TestCase):
    def test_nonprofit_not_flagged(self):
        rc, out, _ = build(body_extra="- The truck association is a non-profit group (B).\n")
        self.assertNotIn("Revenue/profit", out)

    def test_colon_label_style_counts(self):
        report = ("## 8. Licensing\n\n- Fulton County (A: Georgia DPH says plan review is $600).\n\nNot found: x.\n")
        _, warns = br.lint(report, [])
        self.assertFalse(any("No A/B/C label" in w for w in warns), warns)


class Freshness(unittest.TestCase):
    def make(self, d, started, section_days_ago=None, write_run=True, text=None):
        make_sections(d)
        if write_run:
            with open(os.path.join(d, "run.json"), "w") as f:
                json.dump({"started": started, "skill_version": "x"}, f)
        files = sorted(glob.glob(os.path.join(d, "sections", "*.md")))
        if section_days_ago is not None:
            old = time.time() - section_days_ago * 86400
            os.utime(files[9], (old, old))                     # 09-module-8-licensing.md
        if text is not None:
            with open(files[4], "a", encoding="utf-8") as f:
                f.write("\n" + text + "\n")
        return files

    def test_section_older_than_run_start_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            files = self.make(d, dt.date.today().isoformat(), section_days_ago=7)
            errs, _ = br.check_freshness(d, files)
        self.assertEqual(len(errs), 1)
        self.assertIn("09-module-8-licensing.md", errs[0])
        self.assertIn("older run", errs[0])

    def test_all_sections_from_this_run_is_clean(self):
        with tempfile.TemporaryDirectory() as d:
            files = self.make(d, dt.date.today().isoformat())
            errs, warns = br.check_freshness(d, files)
        self.assertEqual((errs, warns), ([], []))

    def test_missing_run_json_warns_only(self):
        with tempfile.TemporaryDirectory() as d:
            files = self.make(d, "", write_run=False, section_days_ago=7)
            errs, warns = br.check_freshness(d, files)
        self.assertEqual(errs, [])
        self.assertTrue(any("run.json" in w for w in warns))

    def test_stale_date_claims_warn(self):
        stale = (dt.date.today() - dt.timedelta(days=7)).isoformat()
        with tempfile.TemporaryDirectory() as d:
            files = self.make(d, dt.date.today().isoformat(), text=f"Today is {stale}. Verified {stale} on the page.")
            _, warns = br.check_freshness(d, files)
        self.assertTrue(any("Today is" in w for w in warns))
        self.assertTrue(any("Verified" in w for w in warns))

    def test_full_build_fails_on_leftover_section(self):
        with tempfile.TemporaryDirectory() as d:
            self.make(d, dt.date.today().isoformat(), section_days_ago=7)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                old = sys.argv
                sys.argv = ["build_report.py", d]
                try:
                    rc = br.main()
                finally:
                    sys.argv = old
        self.assertEqual(rc, 1)
        self.assertIn("left over from an older run", buf.getvalue())


class EstimateRules(unittest.TestCase):
    def lint_with(self, heading, body):
        report = f"## {heading}\n\n{body}\n\nNot found: x.\n"
        return br.lint(report, [])[1]

    def test_est_with_method_in_allowed_module_is_fine(self):
        w = self.lint_with("4. Private and catering demand", "About 56,000 weddings a year (est., method: population x 6 per 1,000).")
        self.assertFalse([x for x in w if "est." in x and "uses est" in x], w)

    def test_est_without_method_warns(self):
        w = self.lint_with("3. Events and seasons", "Crowd about 5,000 (est.).")
        self.assertTrue(any("no 'method:'" in x for x in w), w)

    def test_est_in_module_8_or_other_modules_warns(self):
        for h in ("8. Licensing, permits and operating rules", "1. Food truck competition", "2. Population"):
            w = self.lint_with(h, "Fee about $150 (est., method: guess).")
            self.assertTrue(any("estimates belong in Modules 3, 4, 6 and 7" in x for x in w), (h, w))

    def test_est_is_allowed_for_workable_days_in_module_6(self):
        w = self.lint_with("6. Visitors, weather and operating season",
                           "Workable days (est., method: days at or above 50 F times the dry share).")
        self.assertFalse([x for x in w if "uses est" in x], w)

    def test_est_counts_as_a_label(self):
        w = self.lint_with("4. Private and catering demand", "About 56,230 weddings (est., method: rate x population).")
        self.assertFalse(any("No A/B/C label" in x for x in w), w)


class TandoorWarning(unittest.TestCase):
    def test_brief_without_the_tandoor_question_warns(self):
        rc, out, _ = build(brief_numbers=BRIEF_BODY.replace("a tandoor", "an oven"))
        self.assertIn("tandoor / solid-fuel approval", out)

    def test_brief_with_it_is_quiet(self):
        rc, out, _ = build()
        self.assertNotIn("does not mention the tandoor", out)


class TrendClaims(unittest.TestCase):
    def test_trend_word_without_two_years_warns(self):
        rc, out, _ = build(brief_numbers=BRIEF_BODY + "- Takeaway: a small, shrinking licensed truck scene (B).\n")
        self.assertIn("Trend claim without two dated figures", out)

    def test_trend_with_two_dated_figures_is_fine(self):
        rc, out, _ = build(brief_numbers=BRIEF_BODY +
                           "- Licensed trucks fell from 183 in 2020 to 95 in 2025 (B).\n")
        self.assertNotIn("Trend claim", out)

    def test_ordinary_text_not_flagged(self):
        rc, out, _ = build()
        self.assertNotIn("Trend claim", out)


class SourcesHygiene(unittest.TestCase):
    def check(self, sources_md):
        return br.check_sources("## Sources\n\n" + sources_md + "\n\n## Open questions\n\n- none\n")

    def test_two_sources_on_one_line_is_an_error(self):
        errs, _ = self.check("### Web\n13. ABC7 Chicago, festivals 2026 (abc7chicago.com), B. 14. Choose Chicago, Diwali 2024 "
                             "(choosechicago.com), B.")
        self.assertTrue(any("more than one source" in e for e in errs), errs)

    def test_repeated_number_in_a_group_is_an_error_but_restarts_per_group(self):
        errs, _ = self.check("### Pulled data\n1. a.csv\n1. b.csv\n### Web\n1. x (x.gov)\n")
        self.assertEqual(len(errs), 1)
        self.assertIn("used twice", errs[0])

    def test_clean_sources_pass(self):
        errs, warns = self.check("### Pulled data\n1. noaa.csv, A\n2. acs.csv, A\n### Web\n"
                                 "3. City of Chicago, Food Trucks (https://www.chicago.gov/x.html), A\n"
                                 "4. Roaming Hunger, Spicy Desi (roaminghunger.com), B\n")
        self.assertEqual((errs, warns), ([], []))

    def test_web_source_without_url_warns(self):
        _, warns = self.check("### Web\n1. City Cast Chicago, food truck scene, B\n2. Other search-snippet sources (all C): x\n")
        self.assertEqual(len([w for w in warns if "no URL" in w]), 1)

    def test_snippet_overuse_warns(self):
        text = "## Sources\n\n### Web\n1. x (x.gov), A\n\n" + ("(search snippet, C) " * 11)
        _, warns = br.check_sources(text)
        self.assertTrue(any("search snippets" in w for w in warns))

    def test_estimate_revision_explanation_warns(self):
        _, warns = br.lint("## 2. Population\n\nThe drop reflects ACS estimate revisions (A).\n\nNot found: x.\n", [])
        self.assertTrue(any("estimate revisions" in w for w in warns))
        _, warns = br.lint("## 2. Population\n\nThe drop is small (A).\n\nNot found: x.\n", [])
        self.assertFalse(any("estimate revisions" in w for w in warns))
