import contextlib
import io
import os
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import pdf_text as pt  # noqa: E402

REAL = os.path.join(HERE, "fixtures", "columbus_advisory_minutes.pdf")


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pt.main(list(argv))
    return rc, buf.getvalue()


def synthetic(text, compress=False):
    stream = f"BT /F1 12 Tf ({text}) Tj ET".encode()
    body = zlib.compress(stream) if compress else stream
    return b"%%PDF-1.4\n1 0 obj\n<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream\nendobj\n%%EOF"


LONG = ("Mobile food vending licence fee schedule for the city. The application fee is paid once and the "
        "licence must be renewed every year; propane cylinders are limited to forty pounds per vehicle.")


class Pdf(unittest.TestCase):
    def test_builtin_plain_and_flate(self):
        for c in (False, True):
            self.assertIn("limited to forty pounds", pt.builtin_text(synthetic(LONG, c)))

    def test_builtin_tj_array(self):
        pdf = b"%PDF-1.4\nstream\nBT [(Fire) -300 (Safety) -250 (Permit)] TJ ET\nendstream"
        self.assertEqual(pt.builtin_text(pdf), "Fire Safety Permit")

    def test_real_pdf_extracts_and_greps(self):
        rc, out = run(REAL, "--grep", "vending", "--context", "60", "--max", "600")
        self.assertEqual(rc, 0, out)
        self.assertIn("text extracted with", out)
        self.assertRegex(out.lower(), "vending")

    def test_real_pdf_builtin_reader_only(self):
        with open(REAL, "rb") as f:
            text = pt.builtin_text(f.read())
        self.assertTrue(pt.looks_readable(text) or text == "")   # the fallback may give up, never garbage

    def test_garbled_is_rejected(self):
        self.assertFalse(pt.looks_readable("�" * 200))
        self.assertFalse(pt.looks_readable("xx"))
        self.assertTrue(pt.looks_readable(LONG))

    def test_not_a_pdf_and_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.pdf")
            with open(p, "w") as f:
                f.write("<html>404</html>")
            rc, out = run(p)
            self.assertEqual(rc, 1)
            self.assertIn("not a PDF", out)
        rc, out = run("/nonexistent/file.pdf")
        self.assertEqual(rc, 1)
        self.assertIn("UNREADABLE", out)

    def test_scanned_pdf_says_unread(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "scan.pdf")
            with open(p, "wb") as f:
                f.write(b"%PDF-1.4\nstream\n\x00\x01\x02\nendstream")
            rc, out = run(p)
        self.assertEqual(rc, 1)
        self.assertIn("unread", out.lower())


if __name__ == "__main__":
    unittest.main()


class SavedPagesAndFolders(unittest.TestCase):
    HTML = ("<html><head><title>x</title><script>var a=1;</script></head><body><h1>Food Trucks</h1>"
            "<p>A Mobile Food Preparer license costs <b>$1,000</b> for two years.</p>"
            "<table><tr><td>Propane</td><td>40 lb</td></tr></table></body></html>")

    def test_saved_html_page(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "foodtrucks.html")
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.HTML)
            rc, out = run(p, "--grep", "$1,000", "--context", "30")
        self.assertEqual(rc, 0)
        self.assertIn("saved web page", out)
        self.assertIn("$1,000 for two years", out)
        self.assertNotIn("var a", out)

    def test_folder_reads_every_supported_file_and_flags_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.html"), "w", encoding="utf-8") as f:
                f.write(self.HTML)
            with open(os.path.join(d, "b.txt"), "w", encoding="utf-8") as f:
                f.write("Rule 4: no solid fuel on vehicles without approval.")
            with open(os.path.join(d, "scan.pdf"), "wb") as f:
                f.write(b"%PDF-1.4\nstream\n\x00\x01\nendstream")
            with open(os.path.join(d, "photo.jpg"), "wb") as f:
                f.write(b"\xff\xd8")
            rc, out = run(d)
        self.assertEqual(rc, 0)
        self.assertIn("== a.html [saved web page]", out)
        self.assertIn("== b.txt [text file]", out)
        self.assertIn("== scan.pdf", out)
        self.assertIn("UNREADABLE", out)
        self.assertNotIn("photo.jpg", out)

    def test_empty_folder(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = run(d)
        self.assertEqual(rc, 1)
        self.assertIn("EMPTY", out)

    def test_folder_grep_applies_to_each_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "b.txt"), "w", encoding="utf-8") as f:
                f.write("x " * 200 + "tandoor approval needed " + "y " * 200)
            rc, out = run(d, "--grep", "tandoor", "--context", "20")
        self.assertIn("tandoor approval needed", out)
        self.assertLess(len(out), 300)
