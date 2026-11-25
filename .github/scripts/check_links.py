#!/usr/bin/env python3
"""Check the links in README.md.

Relative links and #anchors must resolve, or the check fails. http(s) links are fetched
concurrently inside a total time budget; one that fails, or isn't reached before the budget
runs out, is printed as a GitHub warning and doesn't fail the check, so an outside site being
down never blocks a merge.

    python3 .github/scripts/check_links.py [file.md ...]
"""
import concurrent.futures as cf
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import unquote

REFDEF = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$", re.M)
HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")
CODESPAN = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.S)
HTML_ANCHOR = re.compile(r"<a\s[^>]*?\b(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.I)
SCHEME = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:|//)", re.I)
ATTEMPTS, TIMEOUT, BUDGET, WORKERS = 3, 15, 150, 8
UA = "Mozilla/5.0 (compatible; readme-link-check)"


def strip_fences(text):
    """Blank out fenced code blocks (``` or ~~~); a fence closes on the same marker, >= as long."""
    out, fence = [], None
    for line in text.splitlines():
        if fence is None:
            m = FENCE_OPEN.match(line)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
                out.append("")
            else:
                out.append(line)
            continue
        out.append("")
        m = FENCE_CLOSE.match(line)
        if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
            fence = None
    return "\n".join(out)


def slug(heading):
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    s = CODESPAN.sub(r"\2", s)
    s = re.sub(r"<[^>]+>", "", s).strip().lower()
    return re.sub(r"[^\w\- ]", "", s).replace(" ", "-")


def anchors_of(text):
    """Heading ids in document order (GitHub's -1, -2 suffixes on repeats) plus <a id/name>."""
    text = strip_fences(text)
    ids, seen = set(), {}
    for h in HEADING.findall(text):
        base = slug(h)
        n = seen.get(base, 0)
        cand = base if n == 0 else "%s-%d" % (base, n)
        while cand in ids:
            n += 1
            cand = "%s-%d" % (base, n)
        seen[base] = n + 1
        ids.add(cand)
    ids.update(a.lower() for a in HTML_ANCHOR.findall(CODESPAN.sub(" ", text)))
    return ids


def _dest(text, i):
    """Parse an inline link destination starting after '](' ; return it or None."""
    n, ws = len(text), " \t\n"
    while i < n and text[i] in ws:
        i += 1
    if i < n and text[i] == "<":
        j, k = text.find(">", i + 1), text.find("\n", i + 1)
        if j < 0 or 0 <= k < j:
            return None
        dest, i = text[i + 1:j], j + 1
    else:
        start, depth = i, 0
        while i < n:
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if c in ws:
                break
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    break
                depth -= 1
            i += 1
        if depth:
            return None
        dest = text[start:i]
    while i < n and text[i] in ws:
        i += 1
    if i < n and text[i] in "\"'(":
        j = text.find(")" if text[i] == "(" else text[i], i + 1)
        if j < 0:
            return None
        i = j + 1
        while i < n and text[i] in ws:
            i += 1
    if i < n and text[i] == ")" and dest:
        return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", dest)
    return None


def link_targets(text):
    """Inline link/image destinations and reference definitions, outside code."""
    text = CODESPAN.sub(" ", strip_fences(text))
    out, i = [], 0
    while (i := text.find("](", i)) >= 0:
        i += 2
        d = _dest(text, i)
        if d:
            out.append(d)
    return out + REFDEF.findall(text)


def find_root(path):
    d = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.getcwd()
        d = parent


def is_timeout(e):
    return isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)


def check_url(url, deadline):
    """Return None if the URL answers below 400, else an error string. Never raises."""
    err = None
    for i in range(ATTEMPTS):
        for method in ("HEAD", "GET"):
            left = deadline - time.monotonic()
            if left <= 0:
                return err or "check budget exhausted"
            try:
                req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=max(1, min(TIMEOUT, left))) as r:
                    if r.status < 400:
                        return None
                    err = "HTTP %d" % r.status
            except urllib.error.HTTPError as e:
                err = "HTTP %d" % e.code
            except ValueError as e:  # malformed URL: retrying can't help
                return "invalid URL: %s" % e
            except Exception as e:  # DNS, TLS, reset: may be transient
                if is_timeout(e):  # a hanging host won't answer a GET either
                    return "timed out"
                err = "%s: %s" % (type(e).__name__, e)
        if i + 1 < ATTEMPTS:
            time.sleep(max(0, min(2 ** i * 2, deadline - time.monotonic())))
    return err


def check_external(urls, budget=BUDGET):
    """Map url -> error-or-None for every URL finished inside the budget; others are absent."""
    deadline = time.monotonic() + budget
    results = {}
    ex = cf.ThreadPoolExecutor(max_workers=WORKERS)
    futs = {ex.submit(check_url, u, deadline): u for u in urls}
    try:
        for f in cf.as_completed(futs, timeout=budget):
            results[futs[f]] = f.result()
    except cf.TimeoutError:
        pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return results


def check_local(path, root=None, cache=None):
    """Check relative links and anchors; return (checked, errors, external urls)."""
    cache = {} if cache is None else cache
    root = root or find_root(path)
    path = os.path.abspath(path)
    base = os.path.dirname(path)

    def anchors(p):
        if p not in cache:
            with open(p, encoding="utf-8") as f:
                cache[p] = anchors_of(f.read())
        return cache[p]

    with open(path, encoding="utf-8") as f:
        text = f.read()
    errors, externals, checked = [], [], 0
    for url in dict.fromkeys(link_targets(text)):
        if url.startswith(("http://", "https://")):
            checked += 1
            externals.append(url)
            continue
        if SCHEME.match(url):  # mailto:, tel:, protocol-relative and the like
            continue
        checked += 1
        target, _, frag = url.partition("#")
        target, frag = unquote(target.partition("?")[0]), unquote(frag)
        if not target:
            full = path
        elif target.startswith("/"):
            full = os.path.join(root, target.lstrip("/"))
        else:
            full = os.path.join(base, target)
        full = os.path.normpath(full)
        err = None
        if not os.path.exists(full):
            err = "doesn't exist"
        elif frag and os.path.isfile(full) and full.lower().endswith((".md", ".markdown")):
            if frag.lower() not in anchors(full) and frag.lower() != "top":
                err = "no heading for #%s in %s" % (frag, target or "this file")
        if err:
            errors.append("%s: %s %s" % (path, url, err))
        else:
            print("ok   %s" % url)
    return checked, errors, externals


def main(paths, budget=BUDGET):
    total, errors, externals, cache = 0, [], [], {}
    for p in paths or ["README.md"]:
        n, e, x = check_local(p, cache=cache)
        total, errors, externals = total + n, errors + e, externals + x
    externals = list(dict.fromkeys(externals))
    results = check_external(externals, budget)
    warnings = []
    for u in externals:
        if u not in results:
            warnings.append("external link not checked, %ds budget used up: %s" % (budget, u))
        elif results[u]:
            warnings.append("external link failed: %s (%s)" % (u, results[u]))
        else:
            print("ok   %s" % u)
    for w in warnings:
        print("::warning::%s" % w)
    for e in errors:
        print("::error::%s" % e)
    print("%d links: %d broken, %d external warnings" % (total, len(errors), len(warnings)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
