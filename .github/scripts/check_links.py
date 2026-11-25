#!/usr/bin/env python3
"""Check the links in README.md.

Relative links and #anchors must resolve, or the check fails. http(s) links are fetched with
retries; one that still fails is printed as a GitHub warning and doesn't fail the check, so an
outside site being down never blocks a merge.

    python3 .github/scripts/check_links.py [file.md ...]
"""
import os
import re
import sys
import time
import urllib.error
import urllib.request

INLINE = re.compile(r"(?<!!)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFDEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$", re.M)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)
FENCE = re.compile(r"^```.*?^```", re.M | re.S)
ATTEMPTS, TIMEOUT = 3, 15
UA = "Mozilla/5.0 (compatible; readme-link-check)"


def slug(heading):
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def fetch(url):
    """Return None if the URL answers below 400, else the last error."""
    err = None
    for i in range(ATTEMPTS):
        for method in ("HEAD", "GET"):
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    if r.status < 400:
                        return None
                    err = "HTTP %d" % r.status
            except urllib.error.HTTPError as e:
                err = "HTTP %d" % e.code
            except Exception as e:  # DNS, TLS, timeout: all transient from here
                err = "%s: %s" % (type(e).__name__, e)
        if i + 1 < ATTEMPTS:
            time.sleep(2 ** i * 2)
    return err


def check(path):
    text = open(path, encoding="utf-8").read()
    body = FENCE.sub("", text)
    anchors = {slug(h) for h in HEADING.findall(body)}
    targets = INLINE.findall(body) + IMAGE.findall(body) + REFDEF.findall(body)
    base = os.path.dirname(os.path.abspath(path))
    errors, warnings, checked = [], [], 0
    for url in dict.fromkeys(targets):
        if url.startswith(("mailto:", "tel:")):
            continue
        checked += 1
        if url.startswith(("http://", "https://")):
            err = fetch(url)
            if err:
                warnings.append("%s: %s (%s)" % (path, url, err))
            else:
                print("ok   %s" % url)
            continue
        target, _, frag = url.partition("#")
        if target:
            if not os.path.exists(os.path.normpath(os.path.join(base, target))):
                errors.append("%s: %s doesn't exist" % (path, url))
                continue
        elif frag and frag.lower() not in anchors:
            errors.append("%s: no heading for #%s" % (path, frag))
            continue
        print("ok   %s" % url)
    return checked, errors, warnings


def main(paths):
    total, errors, warnings = 0, [], []
    for p in paths or ["README.md"]:
        n, e, w = check(p)
        total, errors, warnings = total + n, errors + e, warnings + w
    for w in warnings:
        print("::warning::external link failed after %d tries: %s" % (ATTEMPTS, w))
    for e in errors:
        print("::error::%s" % e)
    print("%d links: %d broken, %d external warnings" % (total, len(errors), len(warnings)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
