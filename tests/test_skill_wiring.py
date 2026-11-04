"""Checks that SKILL.md, the docs and the scripts agree with each other."""
import glob
import os
import py_compile
import re
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def frontmatter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md has no frontmatter"
    data, cur = {}, None
    for line in m.group(1).split("\n"):
        if re.match(r"^\s+-\s", line):
            data.setdefault(cur, []).append(re.sub(r"^\s+-\s+", "", line).strip())
        elif ":" in line:
            cur, _, val = line.partition(":")
            data[cur.strip()] = val.strip() or []
    return data


class Wiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = read("SKILL.md")
        cls.fm = frontmatter(cls.skill)

    def test_frontmatter_keys(self):
        for k in ("name", "description", "argument-hint", "disable-model-invocation", "allowed-tools"):
            self.assertIn(k, self.fm)
        self.assertEqual(self.fm["name"], "pizza-karma-market-research")
        self.assertEqual(self.fm["disable-model-invocation"], "true")

    def test_allowed_tools_is_a_list_of_single_tools(self):
        tools = self.fm["allowed-tools"]
        self.assertIsInstance(tools, list)
        for t in tools:
            self.assertRegex(t, r"^[A-Za-z]+(\(.+\))?$", f"bad tool entry: {t}")
        for needed in ("WebSearch", "WebFetch", "Read", "Write", "Edit"):
            self.assertIn(needed, tools)
        py = [t for t in tools if t.startswith("Bash(")]
        self.assertTrue(any("python3" in t for t in py) and any("py -3" in t for t in py))

    def test_referenced_files_exist(self):
        for ref in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/([\w./-]+)", self.skill):
            if ref.endswith("/"):
                continue
            self.assertTrue(os.path.exists(os.path.join(ROOT, ref)), f"missing {ref}")

    def test_no_bare_relative_links(self):
        for target in re.findall(r"\]\(([^)]+)\)", self.skill):
            if target.startswith("http"):
                continue
            self.assertTrue(target.startswith("${CLAUDE_SKILL_DIR}/"), f"relative link: {target}")

    def test_section_names_match_report_format(self):
        in_skill = set(re.findall(r"`(\d\d-[\w-]+\.md)`", self.skill))
        in_format = set(re.findall(r"\|\s*(\d\d-[\w-]+\.md)\s*\|", read("report-format.md")))
        self.assertEqual(in_skill, in_format)
        self.assertEqual(len(in_format), 12)

    def test_hard_rules_come_first(self):
        self.assertLess(self.skill.index("## Hard rules"), self.skill.index("## Workflow"))
        self.assertLess(self.skill.index("## Hard rules"), 6000)   # compaction keeps only the top of the skill

    def test_every_script_command_points_at_a_real_script(self):
        for name in re.findall(r"scripts/([\w]+\.py)", self.skill):
            self.assertTrue(os.path.exists(os.path.join(ROOT, "scripts", name)), name)

    def test_events_and_demand_tools_are_wired_in(self):
        for name in ("pull_events.py", "event_inventory.py", "pdf_text.py", "data_tool.py", "classify_names.py",
                     "competitors.py", "call_sheet.py"):
            self.assertIn(name, self.skill, name)
            self.assertTrue(os.path.exists(os.path.join(ROOT, "scripts", name)), name)
        mods = read("modules.md")
        for word in ("event_inventory.py coverage", "event_inventory.py demand", "events_by_month.csv",
                     "airport_seasonality_index.csv", "business_patterns_metro.csv", "campus_enrollment.csv"):
            self.assertIn(word, mods, word)
        self.assertIn("`est.`", self.skill)
        self.assertIn("run.json", self.skill)

    def test_scripts_compile(self):
        with tempfile.TemporaryDirectory() as d:
            for path in glob.glob(os.path.join(ROOT, "scripts", "*.py")):
                py_compile.compile(path, cfile=os.path.join(d, os.path.basename(path) + "c"), doraise=True)

    def test_modules_and_checklist_present(self):
        mods = read("modules.md")
        for n in range(1, 10):
            self.assertIn(f"## Module {n}:", mods)
        self.assertIn("verbatim quote", mods)
        self.assertGreaterEqual(len(re.findall(r"(?m)^\d+\. ", read("report-format.md").split("## Final checklist")[1])), 12)
        self.assertIn("Top 10 suburbs", read("report-format.md"))
        self.assertIn("Launch sequence", mods)


if __name__ == "__main__":
    unittest.main()
