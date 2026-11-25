#!/usr/bin/env python3
"""Tests for check_links.py: python3 -I .github/scripts/test_check_links.py"""
import contextlib
import io
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_links as cl  # noqa: E402


class Repo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        os.mkdir(os.path.join(self.root, ".git"))

    def write(self, name, text=""):
        p = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def check(self, readme_text, name="README.md"):
        p = self.write(name, readme_text)
        with contextlib.redirect_stdout(io.StringIO()):
            return cl.check_local(p)

    def errors(self, text, **kw):
        return self.check(text, **kw)[1]


class Fragments(Repo):
    def test_fragment_checked_in_target_markdown(self):
        self.write("other.md", "# Real\n")
        self.assertEqual(self.errors("[a](other.md#real)"), [])
        self.assertEqual(len(self.errors("[a](README.md#missing)")), 1)
        self.assertEqual(len(self.errors("[a](other.md#missing)")), 1)

    def test_fragment_on_non_markdown_target_is_not_checked(self):
        self.write("x.txt", "hi")
        self.assertEqual(self.errors("[a](x.txt#L10)"), [])

    def test_duplicate_headings_get_numbered_ids(self):
        t = "# T\n## Usage\n## Usage\n## Usage\n"
        self.assertEqual(cl.anchors_of(t), {"t", "usage", "usage-1", "usage-2"})
        self.assertEqual(len(self.errors(t + "[c](#usage-3)")), 1)
        self.assertEqual(self.errors(t + "[a](#usage-1) [b](#usage-2)"), [])

    def test_html_anchors(self):
        t = '<a id="top-x"></a>\n<a name=\'old\'>x</a>\n[a](#top-x) [b](#old)'
        self.assertEqual(self.errors(t), [])


class Paths(Repo):
    def test_query_stripped(self):
        self.write("a.md", "# A\n")
        self.assertEqual(self.errors("[a](a.md?plain=1#a)"), [])

    def test_unquote_path_and_fragment_separately(self):
        self.write("my file.md", "# Über\n")
        self.write("a+b.md", "")
        self.write("c#d.md", "")
        t = "[1](my%20file.md#%C3%BCber) [2](a+b.md) [3](c%23d.md)"
        self.assertEqual(self.errors(t), [])
        self.assertEqual(len(self.errors("[1](a%2Bb.md) [2](a%20b.md)")), 1)

    def test_leading_slash_is_repo_root(self):
        self.write("docs/page.md", "[r](/README.md) [e](/etc/passwd)")
        self.write("README.md", "")
        with contextlib.redirect_stdout(io.StringIO()):
            _, errors, _ = cl.check_local(os.path.join(self.root, "docs/page.md"))
        self.assertEqual(len(errors), 1)
        self.assertIn("/etc/passwd", errors[0])

    def test_relative_missing_is_error(self):
        self.assertEqual(len(self.errors("[a](nope.md)")), 1)


class Parsing(Repo):
    def targets(self, text):
        return cl.link_targets(text)

    def test_fences_both_markers_and_lengths(self):
        t = "```\n[a](gone1.md)\n```\n~~~\n[b](gone2.md)\n~~~\n````\n```\n[c](gone3.md)\n````\n"
        self.assertEqual(self.targets(t + "[d](real.md)"), ["real.md"])

    def test_fence_not_closed_by_other_marker_or_shorter(self):
        t = "~~~\n[a](gone.md)\n```\n[b](gone.md)\n~~\n[c](gone.md)\n~~~\n[d](kept.md)"
        self.assertEqual(self.targets(t), ["kept.md"])

    def test_code_spans_ignored(self):
        t = "use `[a](gone.md)` and ``x `[b](gone.md)` y`` then [c](kept.md)"
        self.assertEqual(self.targets(t), ["kept.md"])

    def test_balanced_parens(self):
        self.write("guide(v2).md", "")
        self.assertEqual(self.targets("[g](guide(v2).md)"), ["guide(v2).md"])
        self.assertEqual(self.targets('[g](a.md "t") [h](<b c.md>)'), ["a.md", "b c.md"])
        self.assertEqual(self.errors("[g](guide(v2).md)"), [])

    def test_ref_defs_and_images(self):
        self.assertEqual(self.targets("![i](img.png)\n\n[r]: ref.md\n"), ["img.png", "ref.md"])


class External(unittest.TestCase):
    def test_malformed_url_is_an_error_string_not_a_crash(self):
        err = cl.check_url("https://[oops", time.monotonic() + 5)
        self.assertIn("invalid URL", err)

    def test_malformed_url_only_warns(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "README.md")
            with open(p, "w") as f:
                f.write("[x](https://[oops)")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cl.main([p])
        self.assertEqual(code, 0)
        self.assertIn("::warning::", out.getvalue())

    def test_local_error_still_fails_with_external_warning(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "README.md")
            with open(p, "w") as f:
                f.write("[x](https://[oops) [y](missing.md)")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cl.main([p]), 1)

    def test_budget_reports_unreached_links_as_warnings(self):
        orig = cl.check_url
        cl.check_url = lambda url, deadline: time.sleep(2)
        self.addCleanup(setattr, cl, "check_url", orig)
        urls = ["https://example.invalid/%d" % i for i in range(20)]
        t = time.monotonic()
        res = cl.check_external(urls, budget=0.5)
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertLess(len(res), len(urls))

    def test_hanging_hosts_checked_concurrently_without_get_retry(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(64)
        port, conns, held = srv.getsockname()[1], [], []

        def accept():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                held.append(c)
                conns.append(1)

        threading.Thread(target=accept, daemon=True).start()
        self.addCleanup(srv.close)
        old = (cl.TIMEOUT, os.environ.get("no_proxy"), os.environ.get("NO_PROXY"))
        cl.TIMEOUT = 1
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1"

        def restore():
            cl.TIMEOUT = old[0]
            for k, v in (("no_proxy", old[1]), ("NO_PROXY", old[2])):
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(restore)
        urls = ["http://127.0.0.1:%d/%d" % (port, i) for i in range(6)]
        t = time.monotonic()
        res = cl.check_external(urls, budget=30)
        took = time.monotonic() - t
        self.assertLess(took, 5)  # serial would be 6 links x (HEAD+GET) x 3 attempts x 1s
        self.assertEqual(set(res.values()), {"timed out"})
        time.sleep(0.3)
        self.assertEqual(len(conns), 6)  # one HEAD each: no GET, no retry


if __name__ == "__main__":
    unittest.main()
