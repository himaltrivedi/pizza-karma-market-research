import contextlib
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import call_sheet as cs  # noqa: E402


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = cs.main(list(argv))
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


def add(d, item="Tandoor / solid-fuel approval", office="Chicago Fire Department", priority="1",
        phone="312-744-6249", email="", q="Can a tandoor be approved on a mobile food vehicle?", **kw):
    args = ["add", "--data", d, "--priority", priority, "--item", item, "--office", office, "--question", q]
    if phone:
        args += ["--phone", phone]
    if email:
        args += ["--email", email]
    for k, v in kw.items():
        args += [f"--{k}", v]
    return run(*args)


class CallSheet(unittest.TestCase):
    def test_add_and_dedupe(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(add(d)[0], 0)
            rc, out = add(d)
            self.assertIn("Already listed", out)

    def test_validation(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = add(d, phone="", email="", q="Tell me about ovens")
            self.assertEqual(rc, 2)
            self.assertIn("phone number or an email", out)
            self.assertIn("end it with '?'", out)
            rc, out = add(d, priority="0")
            self.assertEqual(rc, 2)
            self.assertIn("priority", out)

    def test_check_requires_the_tandoor_question(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run("check", "--data", d)[0], 1)
            add(d, item="Plan review fee", office="CDPH", priority="2", q="What is the plan review fee?")
            rc, out = run("check", "--data", d)
            self.assertEqual(rc, 1)
            self.assertIn("tandoor", out)
            add(d, cites="Rules Part II")
            self.assertEqual(run("check", "--data", d)[0], 0)

    def test_report_orders_by_priority_and_groups_by_office(self):
        with tempfile.TemporaryDirectory() as d:
            add(d, item="Plan review fee", office="CDPH", priority="3", q="What is the plan review fee?",
                phone="", email="food@cityofchicago.org")
            add(d, cites="Rules Part II, Rules 2, 4, 5", why="Signature product")
            add(d, item="Fire Safety Class fee", office="Chicago Fire Department", priority="2",
                q="What is the Fire Safety Class fee?")
            rc, out = run("report", "--data", d)
        self.assertLess(out.index("Tandoor / solid-fuel approval"), out.index("Fire Safety Class fee"))
        self.assertLess(out.index("Fire Safety Class fee"), out.index("Plan review fee"))
        self.assertEqual(out.count("**Chicago Fire Department**"), 1)         # grouped, not repeated
        self.assertIn("Cite: Rules Part II", out)
        self.assertIn("Why it matters: Signature product", out)
        self.assertIn("1. **Tandoor", out)

    def test_city_argument_finds_the_data_folder(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                rc, out = run("add", "Chicago, IL", "--item", "x", "--office", "o", "--phone", "1", "--question", "Why?")
                self.assertEqual(rc, 0)
                self.assertTrue(os.path.exists(os.path.join("reports", "chicago-il", "data", "questions_for_offices.csv")))
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()
