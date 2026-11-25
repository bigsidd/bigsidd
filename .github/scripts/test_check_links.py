#!/usr/bin/env python3
"""Tests for check_links.py: python3 -I .github/scripts/test_check_links.py"""
import contextlib
import io
import os
import socket
import subprocess
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


class Round2(Repo):
    def test_footnote_definitions_skipped(self):
        self.assertEqual(self.errors("Text[^1].\n\n[^1]: See the docs\n"), [])

    def test_html_comments_hidden_from_links_and_headings(self):
        t = "<!--\n[a](gone.md)\n# Hidden\n-->\n[b](#hidden) <!-- [c](gone2.md) -->"
        self.assertEqual(len(self.errors(t)), 1)
        self.assertEqual(cl.link_targets(t), ["#hidden"])

    def test_setext_headings(self):
        t = "Install\n=======\n\nUsage guide\n-----\n\ntext\n\n---\n\nafter\n"
        self.assertEqual(cl.anchors_of(t), {"install", "usage-guide"})
        self.assertEqual(self.errors(t + "[a](#install) [b](#usage-guide)"), [])
        self.assertEqual(cl.anchors_of("- item\n---\n"), set())

    def test_slug_emphasis_entities_and_snake_case(self):
        self.assertEqual(cl.slug("_Setup_"), "setup")
        self.assertEqual(cl.slug("**Bold** and *it*"), "bold-and-it")
        self.assertEqual(cl.slug("A &amp; B"), "a--b")
        self.assertEqual(cl.slug("snake_case"), "snake_case")
        self.assertEqual(cl.slug("`_x_` call"), "_x_-call")

    def test_bom(self):
        p = os.path.join(self.root, "README.md")
        with open(p, "w", encoding="utf-8-sig") as f:
            f.write("# Top\n[a](#top)\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cl.check_local(p)[1], [])

    def test_indented_code_and_escaped_brackets(self):
        t = "para\n\n    [a](gone.md)\n\n\t[b](gone.md)\n\n- item\n\n    [c](gone.md)\n"
        self.assertEqual(cl.link_targets(t), ["gone.md"])  # only the list continuation
        self.assertEqual(cl.link_targets("\\[x\\](gone.md) \\[y](gone.md) [ok](k.md)"), ["k.md"])
        self.assertEqual(cl.link_targets("line one\n    [lazy](k.md)"), ["k.md"])

    def test_process_exits_within_budget_against_drip_server(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        port, held = srv.getsockname()[1], []

        def drip(c):
            try:
                c.recv(4096)
                c.sendall(b"HTTP/1.1 200 OK\r\n")
                while True:
                    time.sleep(1)
                    c.sendall(b"X-Drip: 1\r\n")
            except OSError:
                pass

        def accept():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                held.append(c)
                threading.Thread(target=drip, args=(c,), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        self.addCleanup(srv.close)
        p = self.write("README.md", "[d](http://127.0.0.1:%d/)\n" % port)
        env = dict(os.environ, no_proxy="127.0.0.1", NO_PROXY="127.0.0.1", LINK_CHECK_BUDGET="3")
        t = time.monotonic()
        r = subprocess.run([sys.executable, "-I", cl.__file__, p], env=env,
                           capture_output=True, text=True, timeout=30)
        took = time.monotonic() - t
        self.assertLess(took, 3 + 5)
        self.assertEqual(r.returncode, 0)
        self.assertIn("::warning::", r.stdout)
        self.assertIn("1 external warnings", r.stdout)


class Round3(Repo):
    def test_html_local_links_checked(self):
        t = ('<p align="center"><img src="./assets/missing-banner.png">'
             '<a href="docs/missing.md">Docs</a></p>')
        self.assertEqual(len(self.errors(t)), 2)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "README.md")
            with open(p, "w") as f:
                f.write(t)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cl.main([p]), 1)

    def test_html_links_ok_srcset_external_and_ignored_regions(self):
        self.write("a.png")
        self.write("b.png")
        t = ('<picture><source srcset="a.png 1x, b.png 2x"><img src="a.png"></picture>\n'
             '<a href="https://example.com/x">x</a> <img src="https://example.com/i.png">\n'
             '`<img src="gone.png">`\n<!-- <a href="gone.md">x</a> -->\n')
        self.assertEqual(self.errors(t), [])
        self.assertEqual(len(self.errors('<source srcset="a.png 1x, nope.png 2x">')), 1)
        _, _, ext = self.check(t)
        self.assertEqual(ext, ["https://example.com/x", "https://example.com/i.png"])

    def test_id_name_on_any_tag(self):
        t = '<div id="contact"></div>\n<h2 id="stack">S</h2>\n<span name=x1></span>\n'
        self.assertEqual(self.errors(t + "[a](#contact) [b](#stack) [c](#x1)"), [])
        self.assertEqual(len(self.errors(t + "[a](#nope)")), 1)

    def test_tag_scan_is_linear(self):
        t0 = time.monotonic()
        cl.anchors_of("<a " * 20000)
        cl.link_targets("<a " * 20000)
        self.assertLess(time.monotonic() - t0, 2)

    def test_non_utf8_target_does_not_crash(self):
        with open(os.path.join(self.root, "legacy.md"), "wb") as f:
            f.write(b"# Caf\xe9\n")
        self.assertEqual(self.errors("[a](legacy.md#caf)"), [])

    def test_user_content_prefix(self):
        self.assertEqual(self.errors("# Foo\n[a](#user-content-foo)"), [])
        self.assertEqual(len(self.errors("# Foo\n[a](#user-content-bar)")), 1)

    def test_reference_links_in_headings(self):
        self.assertEqual(cl.slug("[Foo][bar]"), "foo")
        self.assertEqual(cl.slug("[Foo][] and [x](y)"), "foo-and-x")


class Round4(Repo):
    def test_srcset_commas_inside_urls(self):
        u1 = "https://github-readme-stats.vercel.app/api?username=x&hide=contribs,prs&theme=dark"
        u2 = "https://skillicons.dev/icons?i=js,ts,py"
        got = cl.link_targets('<img srcset="%s 1x, %s 2x"><img src="%s">' % (u1, u2, u2))
        self.assertEqual(got, [u1, u2, u2])
        self.assertEqual(cl.link_targets('<source srcset="a.png,b.png 2x">'), ["a.png,b.png"])

    def test_heading_with_leading_image_or_tag_keeps_hyphen(self):
        self.write("x.gif")
        self.write("x.png")
        t = '## <img src="x.gif" width="25"> Tech Stack\n\n## ![chart](x.png) GitHub Stats\n'
        self.assertEqual(cl.anchors_of(t), {"-tech-stack", "-github-stats"})
        self.assertEqual(self.errors(t + "[a](#-tech-stack) [b](#-github-stats)"), [])
        self.assertEqual(len(self.errors(t + "[a](#tech-stack)")), 1)
        self.assertEqual(len(self.errors(t + "[a](#github-stats)")), 1)

    def test_refdef_needs_destination_then_eol_or_title(self):
        self.assertEqual(cl.link_targets("[2024]: Joined Acme as lead"), [])
        self.assertEqual(cl.link_targets('[a]: x.md "T"\n[b]: <y z.md>\n[c]: w.md (T)\n[d]: v.md'),
                         ["x.md", "y z.md", "w.md", "v.md"])

    def test_blockquote_headings(self):
        self.assertEqual(cl.anchors_of("> ## Note\n>\n> Other\n> ---\n"), {"note", "other"})
        self.assertEqual(self.errors("> ## Note\n\n[a](#note)"), [])

    def test_perf_long_heading_and_bracket_runs(self):
        for text in ("# x" + " " * 3000 + "y\n", "#" + " " * 3000 + "\n", "a](" * 20000,
                     "[a](" * 15000, "[" * 20000 + "](x" * 20000):
            t0 = time.monotonic()
            cl.anchors_of(text)
            cl.link_targets(text)
            self.assertLess(time.monotonic() - t0, 1, repr(text[:12]))


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
