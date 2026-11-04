#!/usr/bin/env python3
"""
pdf_text.py - turn a document that is ALREADY ON YOUR DISK into text, so you can read and quote it.
Works on PDFs, saved web pages (.html/.htm) and .txt files. When WebFetch hands back a PDF it saves the file and
prints its path ("saved to ...pdf"): run this on that path. The owner can also save pages that block automated
tools (open them in a browser, then File > Save Page As / Print to PDF) into reports/<slug>/manual/ and you read
that whole folder. It reads local files only and never touches the network.

  python3 pdf_text.py FILE.pdf|FILE.html [--grep WORD] [--context 300] [--max 20000] [--pages 1-5]
  python3 pdf_text.py reports/chicago-il/manual/ [--grep WORD]       every supported file in a folder

It uses, in order: the `pdftotext` program if installed (best), the `pypdf` package if installed,
then a small built-in reader that handles plain text PDFs only. If the output is empty or
garbled (scanned pages, unusual fonts), say so: treat that PDF as unread.
Standard library only (pypdf and pdftotext are optional).
"""
import argparse
import html.parser
import os
import re
import shutil
import subprocess
import sys
import zlib


def _unescape(s: bytes) -> bytes:
    s = re.sub(rb"\\([0-7]{1,3})", lambda m: bytes([int(m.group(1), 8) & 255]), s)
    return (s.replace(rb"\(", b"(").replace(rb"\)", b")").replace(rb"\\", b"\\")
            .replace(rb"\n", b"\n").replace(rb"\r", b"").replace(rb"\t", b" "))


def builtin_text(data: bytes) -> str:
    """Plain-text PDFs only: Flate or uncompressed content streams with Tj/TJ strings."""
    chunks = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        body = m.group(1)
        try:
            body = zlib.decompress(body)
        except Exception:
            pass
        if b"BT" not in body:
            continue
        for bt in re.findall(rb"BT(.*?)ET", body, re.S):
            line = []
            for tok in re.finditer(rb"\[(.*?)\]\s*TJ|\((.*?)(?<!\\)\)\s*Tj|(T\*|Td|TD|')", bt, re.S):
                if tok.group(1) is not None:
                    for txt, kern in re.findall(rb"\((.*?)(?<!\\)\)|(-?\d+\.?\d*)", tok.group(1), re.S):
                        if txt:
                            line.append(_unescape(txt))
                        elif kern and float(kern) < -200:
                            line.append(b" ")
                elif tok.group(2) is not None:
                    line.append(_unescape(tok.group(2)))
                else:
                    line.append(b"\n")
            chunks.append(b"".join(line).decode("latin-1", "replace"))
    text = "\n".join(chunks)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


class _HtmlText(html.parser.HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol"}
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_text(raw: str) -> str:
    p = _HtmlText()
    p.feed(raw)
    t = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r" ?\n ?", "\n", t)).strip()


SUPPORTED = (".pdf", ".html", ".htm", ".txt", ".md")


def looks_readable(text: str) -> bool:
    """Reject empty or garbled output (control characters, replacement marks, no real words)."""
    if len(text) < 80:
        return False
    bad = sum(1 for c in text if c == "\ufffd" or (not c.isprintable() and c not in "\n\t\r"))
    words = re.findall(r"[A-Za-z]{3,}", text)
    nonspace = [c for c in text if not c.isspace()]
    alpha = sum(c.isalpha() for c in nonspace) / max(1, len(nonspace))
    return bad / len(text) < 0.03 and len(words) >= 15 and alpha > 0.5


def extract(path: str, pages: str = ""):
    """Returns (text, method)."""
    exe = shutil.which("pdftotext")
    if exe:
        cmd = [exe, "-layout"]
        if pages:
            a, _, b = pages.partition("-")
            cmd += ["-f", a, "-l", b or a]
        r = subprocess.run(cmd + [path, "-"], capture_output=True, timeout=120)
        txt = r.stdout.decode("utf-8", "replace").strip()
        if looks_readable(txt):
            return txt, "pdftotext"
    try:
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(path)
        sel = range(len(reader.pages))
        if pages:
            a, _, b = pages.partition("-")
            sel = range(int(a) - 1, min(len(reader.pages), int(b or a)))
        txt = "\n\n".join((reader.pages[i].extract_text() or "") for i in sel).strip()
        if looks_readable(txt):
            return txt, "pypdf"
    except ImportError:
        pass
    except Exception:
        pass
    with open(path, "rb") as f:
        data = f.read()
    txt = builtin_text(data)
    return (txt, "built-in reader") if looks_readable(txt) else ("", "none")


def file_text(path: str, pages: str = ""):
    """(text, method) for one supported file."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".html", ".htm"):
        with open(path, "rb") as f:
            raw = f.read().decode("utf-8", "replace")
        t = html_text(raw)
        return (t, "saved web page") if len(t) >= 40 else ("", "none")
    if ext in (".txt", ".md"):
        with open(path, encoding="utf-8", errors="replace") as f:
            t = f.read().strip()
        return (t, "text file") if t else ("", "none")
    with open(path, "rb") as f:
        if f.read(5) != b"%PDF-":
            return "", "not a PDF"
    return extract(path, pages)


def cut_to_grep(text: str, word: str, context: int) -> str:
    spans = []
    for m in re.finditer(re.escape(word), text, re.I):
        lo, hi = max(0, m.start() - context), min(len(text), m.end() + context)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = hi
        else:
            spans.append([lo, hi])
    return "\n...\n".join(text[lo:hi] for lo, hi in spans) or f"(no match for {word!r})"


def main_folder(a) -> int:
    names = sorted(n for n in os.listdir(a.file) if n.lower().endswith(SUPPORTED))
    if not names:
        print(f"EMPTY: no .pdf/.html/.txt files in {a.file}")
        return 1
    bad = 0
    for n in names:
        text, method = file_text(os.path.join(a.file, n), a.pages)
        if not text:
            print(f"== {n}\nUNREADABLE ({method}): treat as unread\n")
            bad += 1
            continue
        if a.grep:
            text = cut_to_grep(text, a.grep, a.context)
        print(f"== {n} [{method}]\n{text[: a.max]}" + (f"\n[... cut at {a.max} characters]" if len(text) > a.max else "") + "\n")
    return 0 if bad < len(names) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--grep")
    ap.add_argument("--context", type=int, default=300)
    ap.add_argument("--max", type=int, default=20000)
    ap.add_argument("--pages", default="", help="e.g. 3 or 2-5 (pdftotext and pypdf only)")
    a = ap.parse_args(argv)
    if os.path.isdir(a.file):
        return main_folder(a)
    try:
        open(a.file, "rb").close()
    except OSError as e:
        print(f"UNREADABLE: {e}")
        return 1
    text, method = file_text(a.file, a.pages)
    if method == "not a PDF":
        print("UNREADABLE: not a PDF file")
        return 1
    if not text:
        print("UNREADABLE: no readable text found (scanned pages or unsupported fonts). "
              "Treat this PDF as unread: label what you know C and write `no quote`.")
        return 1
    if a.grep:
        text = cut_to_grep(text, a.grep, a.context)
    print(f"[text extracted with {method}]")
    print(text[: a.max] + (f"\n[... cut at {a.max} characters: use --grep, --pages or --max]" if len(text) > a.max else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
