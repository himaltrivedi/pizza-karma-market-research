import contextlib
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import data_tool as dtool  # noqa: E402

SCHOOLS = os.path.join(HERE, "fixtures", "schools_IL_sample.csv")


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        dtool.main(list(argv))
    return buf.getvalue()


class DataTool(unittest.TestCase):
    def test_columns(self):
        out = run("columns", SCHOOLS)
        self.assertIn("60 rows", out)
        self.assertIn("lea_name", out)

    def test_filter_equals_and_cols(self):
        out = run("filter", SCHOOLS, "--col", "cbsa", "--equals", "16980", "--cols", "lea_name,county_name,enrollment")
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("30 of 60"), first)
        self.assertEqual(out.splitlines()[1], "lea_name\tcounty_name\tenrollment")

    def test_filter_numeric_sort_desc(self):
        out = run("filter", SCHOOLS, "--col", "enrollment", "--gte", "1", "--sort", "enrollment", "--desc",
                  "--cols", "lea_name,enrollment", "-n", "3")
        nums = [float(l.split("\t")[1]) for l in out.splitlines()[2:5]]
        self.assertEqual(nums, sorted(nums, reverse=True))

    def test_count_by(self):
        out = run("count-by", SCHOOLS, "--col", "cbsa")
        self.assertTrue(out.splitlines()[0].endswith("16980") or "16980" in out)

    def test_summary_numeric(self):
        out = run("summary", SCHOOLS, "--cols", "enrollment")
        self.assertIn("enrollment: min", out)

    def test_cells_are_cut(self):
        out = run("head", SCHOOLS, "-n", "2")
        self.assertTrue(all(len(c) <= 60 for l in out.splitlines() for c in l.split("\t")))

    def test_sum_by(self):
        out = run("sum-by", SCHOOLS, "--col", "county_name", "--sum", "enrollment", "-n", "2")
        lines = out.splitlines()
        self.assertEqual(lines[0], "county_name\trows\tsum_enrollment\tmean")
        self.assertTrue(float(lines[1].split("\t")[2]) >= float(lines[2].split("\t")[2]))

    def test_unknown_column_exits(self):
        with self.assertRaises(SystemExit):
            run("head", SCHOOLS, "--cols", "nope")


if __name__ == "__main__":
    unittest.main()
