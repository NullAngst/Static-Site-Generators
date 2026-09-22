#!/usr/bin/env python3
"""
SiteGen: a local static site generator with a markdown editor and live preview.

Run it:
    python3 sitegen.py                 opens the last site you used
    python3 sitegen.py ~/my-site       opens (or creates) a site in that folder
    python3 sitegen.py --help

It needs Python 3.8 or newer and nothing else (standard library only).
It starts a small web server on 127.0.0.1 and opens the editor in your browser.
The editor itself uses JavaScript. The sites it generates use none: only HTML and CSS.

Folder layout (inside the site folder you choose):
    index.html, about.html, ...     generated pages
    blog/index.html                 generated blog feed
    blog/posts/<slug>.html          generated published posts
    style.css                       your stylesheet (edited through "Edit CSS", never overwritten)
    assets/                         images you add from the editor
    .sitegen/                       your markdown sources, drafts and settings (this is what
                                    lets the app re-import the site when you open the folder)

Do not hand-edit the generated .html files, they are rewritten on every save.
Edit style.css freely (or through the editor), it is yours.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import email.utils
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import traceback
import unicodedata
import webbrowser
from html import escape as _escape, unescape as _unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

META_DIR = ".sitegen"
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.RLock()
CURRENT = {"site": None}
SERVER = {"httpd": None, "port": 0}
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "sitegen"
RESERVED_SLUGS = {"index", "style", "assets", "blog", "post", "tag", "rss"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}
MAX_UPLOAD = 25 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def esc(s):
    return _escape(s, quote=False)


def attr(s):
    return _escape(s, quote=True)


_ENTITY_AMP = re.compile(r"&(?!#?\w+;)")


def esc_text(s):
    """Escape text but leave existing entities such as &copy; alone."""
    return _ENTITY_AMP.sub("&amp;", s).replace("<", "&lt;").replace(">", "&gt;")


def slugify(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:80].strip("-")


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def clean_date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10]).isoformat()
    except Exception:
        return datetime.date.today().isoformat()


def nice_date(s):
    d = datetime.date.fromisoformat(clean_date(s))
    return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")


_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def fix_url(url, prefix):
    """Relative URLs written in markdown are relative to the site root.
    Prefix them so they work from pages that live in sub folders."""
    url = str(url).strip()
    if not url:
        return "#"
    low = re.sub(r"[\s\x00-\x1f]+", "", url.lower())
    if low.startswith(("javascript:", "vbscript:", "data:text")):
        return "#"
    if _SCHEME.match(url) or url.startswith(("#", "/", "//", "?")):
        return url
    while url.startswith("./"):
        url = url[2:]
    return prefix + url


def abs_url(site_url, rel):
    """Join a site's base URL with a site-root-relative path. Empty base -> empty (no absolute URL possible)."""
    site_url = str(site_url or "").strip()
    if not site_url:
        return ""
    return site_url.rstrip("/") + "/" + str(rel).lstrip("/")


def resolve_img(site, rel):
    """Absolute URL for a social image path. Relative paths need the site URL; only http(s) absolutes are kept."""
    rel = str(rel or "").strip()
    if not rel:
        return ""
    if _SCHEME.match(rel):
        return rel if re.match(r"^https?://", rel, re.I) else ""
    return abs_url(site.get("url", ""), rel)


def apply_seo(target, d):
    """Copy the per-page/per-post SEO fields from a request dict onto a page or post record, with validation."""
    if "seo_title" in d:
        target["seo_title"] = str(d.get("seo_title") or "").strip()[:120]
    if "description" in d:
        target["description"] = str(d.get("description") or "").strip()[:320]
    if "image" in d:
        target["image"] = str(d.get("image") or "").strip()[:400]
    if "canonical" in d:
        c = str(d.get("canonical") or "").strip()[:400]
        if c and not re.match(r"^https?://", c, re.I):
            raise ApiError("A canonical URL must start with http:// or https://, or be left blank.")
        target["canonical"] = c
    if "noindex" in d:
        target["noindex"] = bool(d.get("noindex"))
    return target


# ----------------------------------------------------------------------------
# Markdown
# ----------------------------------------------------------------------------

BLOCK_TAGS = set(
    "div p figure figcaption details summary section article aside header footer nav table "
    "thead tbody tr td th ul ol li dl dt dd h1 h2 h3 h4 h5 h6 blockquote hr iframe video audio "
    "picture source form fieldset legend label input textarea select option button center address".split()
)
INLINE_TAGS = set(
    "a abbr b bdi bdo br cite code data del dfn em i img ins kbd mark q s samp small span strong "
    "sub sup time u var wbr".split()
)

LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
HR_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
HEAD_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*).*$")
QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
RAW_RE = re.compile(r"^\s{0,3}<(!--|/?([a-zA-Z][\w-]*))")
_EVENT_ATTR = re.compile(r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_RAW_URL = re.compile(r"""(\s(?:src|href|poster)\s*=\s*)(["'])(.*?)\2""", re.I | re.S)


def clean_raw(s):
    """Best-effort removal of scripts, styles, inline event handlers and javascript: URLs."""
    s = re.sub(r"<\s*(script|style)\b.*?<\s*/\s*\1\s*>", "", s, flags=re.I | re.S)
    s = re.sub(r"<\s*/?\s*(script|style)\b[^>]*>", "", s, flags=re.I)
    s = _EVENT_ATTR.sub("", s)
    s = re.sub(r"(?i)javascript\s*:", "blocked:", s)
    return s


class Markdown:
    def __init__(self, prefix=""):
        self.prefix = prefix
        self.ids = {}

    # ---- entry points
    def render(self, text):
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").expandtabs(4)
        return self.blocks(text.split("\n"))

    def raw(self, s):
        s = clean_raw(s)
        return _RAW_URL.sub(
            lambda m: "%s%s%s%s" % (m.group(1), m.group(2), fix_url(m.group(3), self.prefix), m.group(2)), s
        )

    # ---- blocks
    def unique_id(self, text):
        base = slugify(re.sub(r"<[^>]+>", "", text)) or "section"
        n = self.ids.get(base, 0)
        self.ids[base] = n + 1
        return base if n == 0 else "%s-%d" % (base, n)

    def starts_block(self, line, para=False):
        if FENCE_RE.match(line) or HEAD_RE.match(line) or HR_RE.match(line) or QUOTE_RE.match(line):
            return True
        m = LIST_RE.match(line)
        if m:
            if not para:
                return True
            marker = m.group(2)
            return (not marker[0].isdigit()) or marker[:-1] == "1"
        m = RAW_RE.match(line)
        if m and (m.group(1) == "!--" or (m.group(2) or "").lower() in BLOCK_TAGS):
            return True
        return False

    def blocks(self, lines):
        out = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue

            m = FENCE_RE.match(line)
            if m:
                fence, lang = m.group(1), m.group(2)
                i += 1
                buf = []
                while i < n:
                    s = lines[i].strip()
                    if s.startswith(fence) and set(s) <= set(fence[0]):
                        break
                    buf.append(lines[i])
                    i += 1
                i += 1
                cls = ' class="language-%s"' % attr(lang) if lang else ""
                out.append("<pre><code%s>%s</code></pre>" % (cls, esc("\n".join(buf))))
                continue

            m = HEAD_RE.match(line)
            if m:
                level, text = len(m.group(1)), m.group(2)
                out.append('<h%d id="%s">%s</h%d>' % (level, self.unique_id(text), self.inline(text), level))
                i += 1
                continue

            if HR_RE.match(line):
                out.append("<hr>")
                i += 1
                continue

            if QUOTE_RE.match(line):
                buf = []
                while i < n and QUOTE_RE.match(lines[i]):
                    buf.append(QUOTE_RE.match(lines[i]).group(1))
                    i += 1
                out.append("<blockquote>\n%s\n</blockquote>" % self.blocks(buf))
                continue

            if LIST_RE.match(line):
                html_, i = self.list_block(lines, i)
                out.append(html_)
                continue

            m = RAW_RE.match(line)
            if m and (m.group(1) == "!--" or (m.group(2) or "").lower() in BLOCK_TAGS):
                buf = []
                while i < n and lines[i].strip():
                    buf.append(lines[i])
                    i += 1
                out.append(self.raw("\n".join(buf)))
                continue

            if "|" in line and i + 1 < n and SEP_RE.match(lines[i + 1]):
                html_, i = self.table(lines, i)
                out.append(html_)
                continue

            buf = []
            while i < n and lines[i].strip() and (not buf or not self.starts_block(lines[i], True)):
                buf.append(lines[i].lstrip())
                i += 1
            text = "\n".join(buf).strip()
            html_ = self.inline(text)
            html_ = re.sub(r"(?: {2,}|\\)\n", "<br>\n", html_)
            out.append("<p>%s</p>" % html_)
        return "\n".join(out)

    def list_block(self, lines, i):
        n = len(lines)
        m = LIST_RE.match(lines[i])
        base = len(m.group(1))
        ordered = m.group(2)[0].isdigit()
        start = int(m.group(2)[:-1]) if ordered else 1
        items = []
        loose = False

        def is_sibling(mm):
            return bool(mm) and len(mm.group(1)) == base and mm.group(2)[0].isdigit() == ordered

        while i < n:
            line = lines[i]
            if not line.strip():
                j = i
                while j < n and not lines[j].strip():
                    j += 1
                if j >= n:
                    break
                nxt = lines[j]
                ind = len(nxt) - len(nxt.lstrip())
                if is_sibling(LIST_RE.match(nxt)):
                    loose = True
                    i = j
                    continue
                if ind > base and items:
                    items[-1]["lines"].extend([""] * (j - i))
                    i = j
                    continue
                break
            m = LIST_RE.match(line)
            ind = len(line) - len(line.lstrip())
            if is_sibling(m):
                items.append({"first": m.group(3), "width": base + len(m.group(2)) + 1, "lines": []})
                i += 1
                continue
            if ind > base and items:
                strip = min(ind, items[-1]["width"])
                items[-1]["lines"].append(line[strip:])
                i += 1
                continue
            if m:
                break
            if items and lines[i - 1].strip() and not self.starts_block(line, True):
                items[-1]["lines"].append(line.lstrip())
                i += 1
                continue
            break

        if any("" in it["lines"] for it in items):
            loose = True
        rendered = []
        for it in items:
            content = [it["first"]] + it["lines"]
            task, cls = "", ""
            tm = re.match(r"^\[( |x|X)\]\s+(.*)$", content[0])
            if tm:
                task = '<input type="checkbox" disabled%s> ' % (" checked" if tm.group(1) != " " else "")
                content[0] = tm.group(2)
                cls = ' class="task-item"'
            inner = self.blocks(content)
            if not loose:
                inner = re.sub(r"^<p>(.*?)</p>", r"\1", inner, count=1, flags=re.S)
            rendered.append("<li%s>%s%s</li>" % (cls, task, inner))
        tag = "ol" if ordered else "ul"
        start_attr = ' start="%d"' % start if ordered and start != 1 else ""
        return "<%s%s>\n%s\n</%s>" % (tag, start_attr, "\n".join(rendered), tag), i

    def table(self, lines, i):
        def cells(row):
            row = row.strip()
            if row.startswith("|"):
                row = row[1:]
            if row.endswith("|") and not row.endswith("\\|"):
                row = row[:-1]
            return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]

        head = cells(lines[i])
        aligns = []
        for c in cells(lines[i + 1]):
            left, right = c.startswith(":"), c.endswith(":")
            aligns.append("center" if left and right else "right" if right else "left" if left else "")
        i += 2
        rows = []
        while i < len(lines) and lines[i].strip() and "|" in lines[i]:
            rows.append(cells(lines[i]))
            i += 1

        def cell(tag, text, k):
            a = aligns[k] if k < len(aligns) else ""
            style = ' style="text-align:%s"' % a if a else ""
            return "<%s%s>%s</%s>" % (tag, style, self.inline(text), tag)

        out = ['<div class="table-wrap">', "<table>", "<thead>"]
        out.append("<tr>" + "".join(cell("th", c, k) for k, c in enumerate(head)) + "</tr>")
        out += ["</thead>", "<tbody>"]
        for r in rows:
            out.append("<tr>" + "".join(cell("td", c, k) for k, c in enumerate(r)) + "</tr>")
        out += ["</tbody>", "</table>", "</div>"]
        return "\n".join(out), i

    # ---- inline
    def inline(self, text):
        stash = []
        out = self._inline(text.replace("\x00", ""), stash)
        for _ in range(10):
            if "\x00" not in out:
                break
            out = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], out)
        return out

    def _inline(self, text, stash):
        def keep(s):
            stash.append(s)
            return "\x00%d\x00" % (len(stash) - 1)

        # code spans
        text = re.sub(r"(`+)(.+?)\1", lambda m: keep("<code>%s</code>" % esc(m.group(2).strip())), text, flags=re.S)
        # backslash escapes
        text = re.sub(r"\\([\\`*_{}\[\]()#+.!|~<>-])", lambda m: keep(esc(m.group(1))), text)
        # autolinks
        text = re.sub(
            r"<((?:https?://|mailto:)[^\s<>]+)>",
            lambda m: keep('<a href="%s">%s</a>' % (attr(m.group(1)), esc(m.group(1)))),
            text,
        )

        # inline html (whitelisted tags only)
        def tag(m):
            if m.group(1).lower() in INLINE_TAGS:
                return keep(self.raw(m.group(0)))
            return m.group(0)

        text = re.sub(r"</?([a-zA-Z][\w-]*)(?:\s[^<>]*)?/?>", tag, text)

        # images
        def img(m):
            alt = re.sub(r"\x00\d+\x00", "", m.group(1))
            title = ' title="%s"' % attr(m.group(3)) if m.group(3) else ""
            return keep('<img src="%s" alt="%s"%s loading="lazy">' % (attr(fix_url(m.group(2), self.prefix)), attr(alt), title))

        text = re.sub(r'!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"([^"]*)")?\s*\)', img, text)

        # links
        def link(m):
            title = ' title="%s"' % attr(m.group(3)) if m.group(3) else ""
            inner = self._inline(m.group(1), stash)
            return keep('<a href="%s"%s>%s</a>' % (attr(fix_url(m.group(2), self.prefix)), title, inner))

        text = re.sub(r'\[([^\]]+)\]\(\s*<?([^)\s>]+)>?(?:\s+"([^"]*)")?\s*\)', link, text)

        text = esc_text(text)
        text = re.sub(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", r"<strong><em>\1</em></strong>", text, flags=re.S)
        text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text, flags=re.S)
        text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", text, flags=re.S)
        text = re.sub(r"\*(?=[^\s*])(.+?)(?<=[^\s*])\*", r"<em>\1</em>", text, flags=re.S)
        text = re.sub(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)", r"<em>\1</em>", text, flags=re.S)
        text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", text, flags=re.S)
        return text


def auto_summary(html_):
    m = re.search(r"<p>(.*?)</p>", html_, re.S)
    if not m:
        return ""
    t = re.sub(r"\s+", " ", _unescape(re.sub(r"<[^>]+>", "", m.group(1)))).strip()
    return t if len(t) <= 200 else t[:197].rsplit(" ", 1)[0] + "..."


# ----------------------------------------------------------------------------
# Themes and CSS
# ----------------------------------------------------------------------------

FONT_SANS = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
FONT_SERIF = 'Georgia, "Iowan Old Style", "Palatino Linotype", Palatino, "Times New Roman", serif'
FONT_MONO = 'ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, "DejaVu Sans Mono", monospace'

THEME_DEFAULTS = {
    "bg": "#f6f7f9",
    "bg-image": "none",
    "surface": "#ffffff",
    "surface-opacity": "1",
    "text": "#1f2328",
    "muted": "#59636e",
    "accent": "#0b5fd6",
    "border": "#d5dbe3",
    "font-body": FONT_SANS,
    "font-heading": FONT_SANS,
    "font-mono": FONT_MONO,
    "base-size": "17px",
    "line-height": "1.65",
    "max-width": "46rem",
    "radius": "8px",
    "heading-weight": "700",
    "heading-transform": "none",
    "letter-spacing": "0",
}
VAR_ORDER = list(THEME_DEFAULTS.keys())

THEMES = {
    "clean": ("Clean light", {}),
    "midnight": ("Midnight", {
        "bg": "#0e1116", "surface": "#161b22", "text": "#e6edf3", "muted": "#9aa4b2",
        "accent": "#6cb0ff", "border": "#2b3340",
    }),
    "terminal": ("Terminal", {
        "bg": "#020403", "surface": "#06100a", "surface-opacity": "0.88", "text": "#3dff7a",
        "muted": "#22b555", "accent": "#b6ffcb", "border": "#146b31", "font-body": FONT_MONO,
        "font-heading": FONT_MONO, "base-size": "16px", "line-height": "1.7", "radius": "0px",
        "heading-transform": "uppercase", "letter-spacing": "0.05em", "max-width": "52rem",
    }),
    "paper": ("Paper", {
        "bg": "#e9e6dc", "surface": "#f7f4ea", "text": "#2b2a26", "muted": "#6d6a5e",
        "accent": "#1d5c63", "border": "#cfcabb", "font-body": FONT_SERIF, "font-heading": FONT_SERIF,
        "base-size": "19px", "line-height": "1.75", "radius": "3px", "max-width": "42rem",
    }),
    "nord": ("Nord", {
        "bg": "#2e3440", "surface": "#3b4252", "text": "#eceff4", "muted": "#a7b1c4",
        "accent": "#88c0d0", "border": "#4c566a", "radius": "6px",
    }),
    "neon": ("Neon night", {
        "bg": "#0b0016", "surface": "#150029", "surface-opacity": "0.9", "text": "#f3e8ff",
        "muted": "#b491ff", "accent": "#19f0ff", "border": "#6a1fd0", "font-heading": FONT_MONO,
        "heading-transform": "uppercase", "letter-spacing": "0.06em", "radius": "2px",
    }),
    "solarized": ("Solarized light", {
        "bg": "#fdf6e3", "surface": "#fffbee", "text": "#4b5f66", "muted": "#7a8b90",
        "accent": "#1f78b4", "border": "#e6dcbc",
    }),
    "minimal": ("Minimal serif", {
        "bg": "#ffffff", "surface": "#ffffff", "text": "#151515", "muted": "#666666",
        "accent": "#151515", "border": "#e2e2e2", "font-body": FONT_SERIF, "font-heading": FONT_SANS,
        "base-size": "19px", "line-height": "1.75", "radius": "0px", "max-width": "38rem",
    }),
}


def root_block(theme_id):
    label, over = THEMES[theme_id]
    t = dict(THEME_DEFAULTS)
    t.update(over)
    lines = [":root {", "  /* Theme: %s. Change any value below. */" % label]
    for k in VAR_ORDER:
        lines.append("  --%s: %s;" % (k, t[k]))
    lines.append("}")
    return "\n".join(lines)


CSS_HEADER = """/*
  SiteGen stylesheet

  TOP:     global theme variables (colors, fonts, opacity, sizes). They apply to every page.
  MIDDLE:  base styles that use those variables.
  BOTTOM:  page specific rules. Every page has its own body class, so a rule
           under it only affects that page.

  Tip: "surface-opacity" controls how see-through the content panel and header are.
       "bg-image" takes a value like url(assets/background.jpg).
*/

/* ============ GLOBAL THEME (every page) ============ */
"""

BASE_CSS = """
/* ============ GLOBAL BASE STYLES (every page) ============ */
*, *::before, *::after { box-sizing: border-box; }
html { font-size: var(--base-size); -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background-color: var(--bg);
  background-image: var(--bg-image);
  background-size: cover;
  background-attachment: fixed;
  color: var(--text);
  font-family: var(--font-body);
  line-height: var(--line-height);
}
a { color: var(--accent); text-underline-offset: 0.18em; }
a:hover { text-decoration-thickness: 2px; }
a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.wrap { max-width: var(--max-width); margin: 0 auto; padding: 0 1.25rem; }
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 1rem; top: 1rem; z-index: 10; padding: 0.5rem 0.75rem; background: var(--surface); }

.site-header {
  background: var(--surface);
  background: color-mix(in srgb, var(--surface) calc(var(--surface-opacity) * 100%), transparent);
  border-bottom: 1px solid var(--border);
}
.site-header .wrap {
  display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between;
  gap: 0.5rem 2rem; padding-top: 1rem; padding-bottom: 1rem;
}
.site-title {
  font-family: var(--font-heading); font-weight: var(--heading-weight); font-size: 1.35rem;
  text-transform: var(--heading-transform); letter-spacing: var(--letter-spacing);
  color: var(--text); text-decoration: none;
}
.tagline { margin: 0.15rem 0 0; color: var(--muted); font-size: 0.9rem; }
.site-nav ul { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 0.25rem 1.25rem; }
.site-nav a { color: var(--text); text-decoration: none; padding: 0.2rem 0; border-bottom: 2px solid transparent; }
.site-nav a:hover { border-bottom-color: var(--border); }
.site-nav a[aria-current="page"], .site-nav a.active { color: var(--accent); border-bottom-color: var(--accent); }

main.wrap { padding-top: 2rem; padding-bottom: 3rem; }
.content {
  background: var(--surface);
  background: color-mix(in srgb, var(--surface) calc(var(--surface-opacity) * 100%), transparent);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 2rem clamp(1rem, 4vw, 2.5rem);
}
.content h1, .content h2, .content h3, .content h4, .content h5, .content h6 {
  font-family: var(--font-heading); font-weight: var(--heading-weight);
  text-transform: var(--heading-transform); letter-spacing: var(--letter-spacing); line-height: 1.2;
}
.content h1 { font-size: 2rem; margin: 0 0 1rem; }
.content h2 { font-size: 1.5rem; margin: 2rem 0 0.75rem; }
.content h3 { font-size: 1.2rem; margin: 1.5rem 0 0.5rem; }
.content h4, .content h5, .content h6 { font-size: 1rem; margin: 1.25rem 0 0.5rem; }
.content > :last-child { margin-bottom: 0; }
.content p, .content ul, .content ol { margin: 0 0 1rem; }
.content img, .content video, .content iframe { max-width: 100%; height: auto; border-radius: calc(var(--radius) / 2); }
.content blockquote { margin: 1rem 0; padding: 0.1rem 1rem; border-left: 3px solid var(--accent); color: var(--muted); }
.content code {
  font-family: var(--font-mono); font-size: 0.9em; padding: 0.1em 0.35em;
  background: color-mix(in srgb, var(--text) 9%, transparent); border-radius: calc(var(--radius) / 2);
}
.content pre {
  overflow-x: auto; margin: 0 0 1rem; padding: 1rem; line-height: 1.5;
  background: color-mix(in srgb, var(--text) 7%, transparent);
  border: 1px solid var(--border); border-radius: var(--radius);
}
.content pre code { padding: 0; background: none; }
.table-wrap { overflow-x: auto; margin: 0 0 1rem; }
.content table { border-collapse: collapse; min-width: 100%; }
.content th, .content td { border: 1px solid var(--border); padding: 0.4rem 0.7rem; text-align: left; }
.content hr { border: 0; border-top: 1px solid var(--border); margin: 2rem 0; }
.task-item { list-style: none; margin-left: -1.25rem; }
.task-item input { margin-right: 0.5rem; }

.post-list { list-style: none; margin: 2rem 0 0; padding: 0; }
.post-item { padding: 1.25rem 0; border-top: 1px solid var(--border); }
.post-item:first-child { border-top: 0; padding-top: 0; }
.post-item h2 { margin: 0 0 0.25rem; font-size: 1.35rem; }
.post-item h2 a { color: var(--text); text-decoration: none; }
.post-item h2 a:hover { color: var(--accent); }
.post-meta { margin: 0 0 0.5rem; color: var(--muted); font-size: 0.9rem; }
.post-summary { margin: 0; }
.post-back { margin: 2rem 0 0; }

.site-footer { border-top: 1px solid var(--border); color: var(--muted); font-size: 0.9rem; }
.site-footer .wrap { padding-top: 1.25rem; padding-bottom: 2rem; }
.site-footer p { margin: 0; }
"""

TAG_CSS = """
/* ============ TAGS AND FEED LINKS ============ */
.content .post-tags { list-style: none; margin: 0 0 0.6rem; padding: 0; display: flex; flex-wrap: wrap; gap: 0.35rem; font-size: 0.8rem; }
.post-tags a { display: inline-block; padding: 0 0.6rem; color: var(--muted); border: 1px solid var(--border); border-radius: 99px; text-decoration: none; }
.post-tags a:hover { color: var(--accent); border-color: var(--accent); }
.feed-links, .tag-back { margin: 0 0 1rem; font-size: 0.9rem; }
"""

PAGE_MARK = "/* ============ PAGE SPECIFIC RULES (one section per page) ============ */"
PAGE_HELP = """/*
  Home page:            body.page-index
  Blog feed:            body.page-blog
  Every blog post:      body.page-post
  Every tag page:       body.page-tag   (one tag: body.tag-your-tag)
  One specific post:    body.post-your-post-slug
  Any other page:       body.page-your-page-slug
  Example: body.page-about .content { max-width: 30rem; }
*/
"""


def css_stub(slug, title):
    return "\n/* --- %s (body.page-%s) --- */\nbody.page-%s {\n}\n" % (title, slug, slug)


def default_css(theme="clean"):
    return (
        CSS_HEADER + root_block(theme) + "\n" + BASE_CSS + TAG_CSS + "\n" + PAGE_MARK + "\n" + PAGE_HELP
        + css_stub("index", "Home") + css_stub("post", "All blog posts") + css_stub("tag", "All tag pages")
    )


# ----------------------------------------------------------------------------
# Seed content
# ----------------------------------------------------------------------------

HOME_MD = "Welcome. Edit this page on the left and the preview updates as you type.\n"

ABOUT_MD = """Hi, I'm [your name]. Write a few lines here about who you are and what this site covers.

## What you will find here

Describe the topics you write about or the work you do.

## Get in touch

Add an email address or a link to your profile.
"""

PRIVACY_MD = """Last updated: [DATE]

This page explains what information [SITE] collects when you visit and what happens to it.

## What this site collects

This site is a static website. It does not set cookies, does not run analytics or tracking scripts, and does not ask you to create an account.

## Server logs

The site is hosted by [your hosting provider]. Web hosts commonly keep standard server logs that include IP addresses, browser details, the pages requested and the time of each request. Edit this section to describe what your host actually does.

## Links and embedded content

Pages may link to other websites. Those sites have their own privacy practices and are outside my control.

## Contact

Questions about this policy can be sent to [your email address].

## Changes

If this policy changes, the date at the top of this page will be updated.
"""


# ----------------------------------------------------------------------------
# Site model
# ----------------------------------------------------------------------------

def default_cfg(title):
    return {
        "version": 1,
        "site": {
            "title": title or "My site",
            "tagline": "",
            "footer": "&copy; {year} {title}",
            "author": "",
            "lang": "en",
            "url": "",
            "rss": False,
            "description": "",
            "image": "",
            "twitter": "",
            "noindex": False,
            "sitemap": False,
        },
        "pages": [{"slug": "index", "title": "Home", "kind": "home", "show_title": False}],
        "posts": [],
        "nav": [{"type": "page", "slug": "index", "label": "Home", "visible": True}],
    }


def page_file(page):
    if page["kind"] == "home":
        return "index.html"
    if page["kind"] == "blog":
        return "blog/index.html"
    return page["slug"] + ".html"


def page_prefix(page):
    return "../" if page["kind"] == "blog" else ""


def post_file(slug):
    return "blog/posts/%s.html" % slug


def find_page(cfg, slug):
    return next((p for p in cfg["pages"] if p["slug"] == slug), None)


def published_posts(cfg):
    posts = [p for p in cfg["posts"] if p.get("status") == "published"]
    posts.sort(key=lambda p: (p.get("date", ""), p.get("published_at", "")), reverse=True)
    return posts


def clean_tags(raw, strict=False, known=None):
    """Normalise a list (or comma separated string) of tags. Tags with the same slug are merged,
    and a spelling already used on another post wins so the display name stays consistent."""
    if isinstance(raw, str):
        raw = raw.split(",")
    known = known or {}
    out, seen = [], set()
    for t in raw if isinstance(raw, list) else []:
        t = re.sub(r"\s+", " ", str(t)).strip()[:40]
        if not t:
            continue
        slug = slugify(t)
        if not slug:
            if strict:
                raise ApiError('The tag "%s" needs at least one letter or number.' % t)
            continue
        if slug in seen:
            continue
        seen.add(slug)
        out.append(known.get(slug, t))
    return out[:12]


def tags_html(tags, href_prefix):
    pairs = [(t, slugify(t)) for t in tags]
    pairs = [(t, s) for t, s in pairs if s]
    if not pairs:
        return ""
    items = "".join('<li><a href="%s%s.html">%s</a></li>' % (href_prefix, attr(s), esc(t)) for t, s in pairs)
    return '<ul class="post-tags" aria-label="Tags">%s</ul>\n' % items


def tag_map(cfg):
    """tag slug -> {"name": display name, "posts": [published posts, newest first]}"""
    out = {}
    for p in published_posts(cfg):
        for t in p.get("tags", []):
            s = slugify(t)
            if s:
                out.setdefault(s, {"name": t, "posts": []})
                if p not in out[s]["posts"]:
                    out[s]["posts"].append(p)
    return out


class Site:
    def __init__(self, root):
        self.root = root
        self.meta = root / META_DIR
        self.cfg = None

    # ---- paths
    def page_md(self, slug):
        return self.meta / "pages" / (slug + ".md")

    def p_draft(self, slug):
        return self.meta / "drafts" / (slug + ".md")

    def p_post(self, slug):
        return self.meta / "posts" / (slug + ".md")

    def css_path(self):
        return self.root / "style.css"

    # ---- load and save
    def load(self):
        f = self.meta / "config.json"
        if f.exists():
            self.cfg = json.loads(f.read_text(encoding="utf-8"))
        else:
            self.cfg = default_cfg(re.sub(r"[-_]+", " ", self.root.name).strip().title())
            write_text(self.page_md("index"), HOME_MD)
        (self.root / "assets").mkdir(parents=True, exist_ok=True)
        if not self.css_path().exists():
            write_text(self.css_path(), default_css("clean"))
        self.upgrade_css()
        self.normalize()
        self.save_cfg()
        self.build()

    def upgrade_css(self):
        """Add the tag styles to a SiteGen stylesheet that predates them."""
        css = read_text(self.css_path())
        if ".post-tags" in css or "SiteGen stylesheet" not in css:
            return
        if PAGE_MARK in css:
            css = css.replace(PAGE_MARK, TAG_CSS.strip("\n") + "\n\n" + PAGE_MARK, 1)
        else:
            css = css.rstrip("\n") + "\n" + TAG_CSS
        write_text(self.css_path(), css)

    def save_cfg(self):
        write_text(self.meta / "config.json", json.dumps(self.cfg, indent=2, ensure_ascii=False) + "\n")

    def normalize(self):
        c = self.cfg
        d = default_cfg("")
        c.setdefault("site", {})
        for k, v in d["site"].items():
            c["site"].setdefault(k, v)
        c.setdefault("pages", [])
        c.setdefault("posts", [])
        c.setdefault("nav", [])
        if not find_page(c, "index"):
            c["pages"].insert(0, d["pages"][0])
            if not self.page_md("index").exists():
                write_text(self.page_md("index"), HOME_MD)
        for p in c["pages"]:
            p.setdefault("kind", "page")
            p.setdefault("show_title", p["kind"] != "home")
        slugs = {p["slug"] for p in c["pages"]}
        nav = [n for n in c["nav"] if n.get("type") == "link" or n.get("slug") in slugs]
        have = {n["slug"] for n in nav if n.get("type") == "page"}
        for p in c["pages"]:
            if p["slug"] not in have:
                nav.append({"type": "page", "slug": p["slug"], "label": p["title"], "visible": True})
        c["nav"] = nav

    def state(self):
        c = self.cfg
        return {
            "folder": str(self.root),
            "site": c["site"],
            "pages": c["pages"],
            "posts": c["posts"],
            "nav": c["nav"],
            "themes": [{"id": k, "label": v[0], "block": root_block(k)} for k, v in THEMES.items()],
        }

    # ---- rendering
    def read_post_md(self, slug):
        text = read_text(self.p_post(slug))
        return text if text else read_text(self.p_draft(slug))

    def nav_html(self, cfg, prefix, current, exact=True):
        items = []
        for n in cfg["nav"]:
            if not n.get("visible", True):
                continue
            cur = ""
            if n["type"] == "page":
                pg = find_page(cfg, n["slug"])
                if not pg:
                    continue
                href = prefix + page_file(pg)
                label = n.get("label") or pg["title"]
                if pg["slug"] == current:
                    cur = ' aria-current="page"' if exact else ' class="active"'
            else:
                href = fix_url(n.get("url", ""), prefix)
                label = n.get("label") or n.get("url", "")
            items.append('<li><a href="%s"%s>%s</a></li>' % (attr(href), cur, esc(label)))
        return "\n".join(items)

    @staticmethod
    def _title_pair(site, short, seo_title, is_home=False):
        """Return (browser title, social/OG title). The browser title adds the site name; the social title stays short."""
        st = site["title"]
        social = st if is_home else (short or st)
        if seo_title:
            full = seo_title
        elif is_home or not short or short == st:
            full = st
        else:
            full = "%s | %s" % (short, st)
        return full, social

    def _seo_dict(self, cfg, *, short_title, description, rel, seo_title="", canonical_override="",
                  image_override="", noindex=False, og_type="website", published="", tags=None, is_home=False):
        """Resolve every SEO value a page needs into one dict, applying site-wide fallbacks and building absolute URLs."""
        site = cfg["site"]
        full, social = self._title_pair(site, short_title, seo_title, is_home)
        override = (canonical_override or "").strip()
        canonical = override if re.match(r"^https?://", override, re.I) else abs_url(site.get("url", ""), rel)
        return {
            "full_title": full,
            "social_title": social,
            "description": (description or "").strip(),
            "canonical": canonical,
            "image": resolve_img(site, image_override),
            "og_type": og_type,
            "noindex": bool(noindex),
            "published": published or "",
            "tags": tags or [],
            "rel": rel,
        }

    def head_seo(self, cfg, seo, prefix):
        """Build the <head> SEO tags: title, description, canonical, robots, Open Graph and Twitter cards."""
        site = cfg["site"]
        h = ["<title>%s</title>" % esc(seo["full_title"])]
        if seo["description"]:
            h.append('<meta name="description" content="%s">' % attr(seo["description"]))
        if site.get("author"):
            h.append('<meta name="author" content="%s">' % attr(site["author"]))
        if seo["canonical"]:
            h.append('<link rel="canonical" href="%s">' % attr(seo["canonical"]))
        if seo["noindex"]:
            h.append('<meta name="robots" content="noindex, follow">')
        h.append('<meta property="og:type" content="%s">' % attr(seo["og_type"]))
        h.append('<meta property="og:title" content="%s">' % attr(seo["social_title"]))
        if seo["description"]:
            h.append('<meta property="og:description" content="%s">' % attr(seo["description"]))
        h.append('<meta property="og:site_name" content="%s">' % attr(site["title"]))
        if seo["canonical"]:
            h.append('<meta property="og:url" content="%s">' % attr(seo["canonical"]))
        if site.get("lang"):
            h.append('<meta property="og:locale" content="%s">' % attr(site["lang"].replace("-", "_")))
        if seo["image"]:
            h.append('<meta property="og:image" content="%s">' % attr(seo["image"]))
        if seo["og_type"] == "article":
            if seo["published"]:
                h.append('<meta property="article:published_time" content="%s">' % attr(seo["published"]))
            for t in seo["tags"]:
                if slugify(t):
                    h.append('<meta property="article:tag" content="%s">' % attr(t))
        h.append('<meta name="twitter:card" content="%s">' % ("summary_large_image" if seo["image"] else "summary"))
        h.append('<meta name="twitter:title" content="%s">' % attr(seo["social_title"]))
        if seo["description"]:
            h.append('<meta name="twitter:description" content="%s">' % attr(seo["description"]))
        if seo["image"]:
            h.append('<meta name="twitter:image" content="%s">' % attr(seo["image"]))
        tw = str(site.get("twitter") or "").strip()
        if tw:
            if not tw.startswith("@") and not _SCHEME.match(tw):
                tw = "@" + tw.lstrip("@")
            h.append('<meta name="twitter:site" content="%s">' % attr(tw))
        return h

    def doc(self, cfg, body, prefix, body_class, current, exact=True, css=None, base=None, seo=None):
        site = cfg["site"]
        st = site["title"]
        if seo is None:
            seo = self._seo_dict(
                cfg, short_title=st, description=site.get("description") or site.get("tagline") or "",
                rel="index.html", og_type="website", is_home=True,
            )
        head = ['<meta charset="utf-8">']
        if base:
            head.append('<base href="%s">' % attr(base))
        head.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        head.extend(self.head_seo(cfg, seo, prefix))
        if site.get("rss") and site.get("url"):
            head.append('<link rel="alternate" type="application/rss+xml" title="%s" href="%srss">' % (attr(st), prefix))
        if css is None:
            head.append('<link rel="stylesheet" href="%sstyle.css">' % prefix)
        else:
            head.append("<style>\n%s\n</style>" % css.replace("</", "<\\/"))
        tagline = '<p class="tagline">%s</p>' % esc(site["tagline"]) if site.get("tagline") else ""
        nav = self.nav_html(cfg, prefix, current, exact)
        nav_block = '\n<nav class="site-nav" aria-label="Main"><ul>\n%s\n</ul></nav>' % nav if nav else ""
        header = (
            '<header class="site-header"><div class="wrap">\n'
            '<div class="brand"><a class="site-title" href="%sindex.html">%s</a>%s</div>%s\n</div></header>'
            % (prefix, esc(st), tagline, nav_block)
        )
        foot = site.get("footer", "").replace("{year}", str(datetime.date.today().year)).replace("{title}", st)
        footer = ""
        if foot.strip():
            footer = '<footer class="site-footer"><div class="wrap"><p>%s</p></div></footer>' % Markdown(prefix).inline(foot)
        return (
            '<!DOCTYPE html>\n<html lang="%s">\n<head>\n%s\n</head>\n<body class="%s">\n'
            '<a class="skip" href="#main">Skip to content</a>\n%s\n<main id="main" class="wrap">\n%s\n</main>\n%s\n</body>\n</html>\n'
            % (attr(site.get("lang") or "en"), "\n".join(head), attr(body_class), header, body, footer)
        )

    def feed_html(self, cfg, posts=None, posts_href="posts/", tag_href="tag/"):
        if posts is None:
            posts = published_posts(cfg)
        if not posts:
            return '<p class="empty">No posts yet.</p>'
        items = []
        for p in posts:
            summary = (p.get("summary") or "").strip()
            if not summary:
                summary = auto_summary(Markdown("").render(self.read_post_md(p["slug"])))
            items.append(
                '<li class="post-item">\n<h2><a href="%s%s.html">%s</a></h2>\n'
                '<p class="post-meta"><time datetime="%s">%s</time></p>\n%s%s</li>'
                % (
                    posts_href, attr(p["slug"]), esc(p["title"]), attr(p["date"]), esc(nice_date(p["date"])),
                    tags_html(p.get("tags", []), tag_href),
                    '<p class="post-summary">%s</p>\n' % esc(summary) if summary else "",
                )
            )
        return '<ul class="post-list">\n%s\n</ul>' % "\n".join(items)

    def render_page(self, cfg, page, markdown, css=None, base=None):
        site = cfg["site"]
        prefix = page_prefix(page)
        parts = []
        if page.get("show_title", True):
            parts.append('<h1 class="page-title">%s</h1>' % esc(page["title"]))
        body = Markdown(prefix).render(markdown)
        if body:
            parts.append(body)
        if page["kind"] == "blog":
            if site.get("rss") and site.get("url"):
                parts.append('<p class="feed-links"><a href="../rss">RSS feed</a></p>')
            parts.append(self.feed_html(cfg))
        inner = '<article class="content">\n%s\n</article>' % "\n".join(parts)
        desc = (page.get("description") or "").strip() or site.get("description") or site.get("tagline") or ""
        seo = self._seo_dict(
            cfg, short_title=page["title"], seo_title=page.get("seo_title"), description=desc,
            rel=page_file(page), canonical_override=page.get("canonical"),
            image_override=page.get("image") or site.get("image"),
            noindex=bool(site.get("noindex") or page.get("noindex")), og_type="website",
            is_home=page["kind"] == "home",
        )
        html_ = self.doc(cfg, inner, prefix, "page-" + page["slug"], page["slug"], True, css=css, base=base, seo=seo)
        return html_, seo

    def page_html(self, cfg, page, markdown, css=None, base=None):
        return self.render_page(cfg, page, markdown, css, base)[0]

    def render_post(self, cfg, post, markdown, css=None, base=None):
        site = cfg["site"]
        prefix = "../../"
        body = Markdown(prefix).render(markdown)
        back = ""
        if find_page(cfg, "blog"):
            back = '\n<p class="post-back"><a href="../index.html">Back to all posts</a></p>'
        inner = (
            '<article class="content post">\n<h1 class="page-title">%s</h1>\n'
            '<p class="post-meta"><time datetime="%s">%s</time></p>\n%s%s%s\n</article>'
            % (
                esc(post["title"]), attr(post["date"]), esc(nice_date(post["date"])),
                tags_html(post.get("tags", []), "../tag/"), body, back,
            )
        )
        desc = (post.get("description") or "").strip() or (post.get("summary") or "").strip() or auto_summary(body)
        seo = self._seo_dict(
            cfg, short_title=post["title"], seo_title=post.get("seo_title"), description=desc,
            rel=post_file(post["slug"]), canonical_override=post.get("canonical"),
            image_override=post.get("image") or site.get("image"),
            noindex=bool(site.get("noindex") or post.get("noindex")), og_type="article",
            published=clean_date(post.get("date")), tags=post.get("tags", []),
        )
        html_ = self.doc(cfg, inner, prefix, "page-post post-" + post["slug"], "blog", False, css=css, base=base, seo=seo)
        return html_, seo

    def post_html(self, cfg, post, markdown, css=None, base=None):
        return self.render_post(cfg, post, markdown, css, base)[0]

    def tag_html(self, cfg, name, posts, css=None, base=None):
        site = cfg["site"]
        prefix = "../../"
        inner = (
            '<article class="content">\n<h1 class="page-title">Posts tagged &ldquo;%s&rdquo;</h1>\n'
            '<p class="tag-back"><a href="../index.html">All posts</a></p>\n%s\n</article>'
            % (esc(name), self.feed_html(cfg, posts, "../posts/", ""))
        )
        seo = self._seo_dict(
            cfg, short_title="Tagged: " + name,
            description="Posts tagged \u201c%s\u201d on %s." % (name, site["title"]),
            rel="blog/tag/%s.html" % slugify(name), image_override=site.get("image"),
            noindex=bool(site.get("noindex")), og_type="website",
        )
        return self.doc(cfg, inner, prefix, "page-tag tag-" + slugify(name), "blog", False, css=css, base=base, seo=seo)

    def rss_xml(self, cfg):
        site = cfg["site"]
        base = site["url"].rstrip("/")

        def x(v):
            return _escape(str(v), quote=False)

        items = []
        for p in published_posts(cfg)[:50]:
            link = "%s/blog/posts/%s.html" % (base, p["slug"])
            body = Markdown(base + "/").render(self.read_post_md(p["slug"]))
            summary = (p.get("summary") or "").strip() or auto_summary(body)
            d = datetime.date.fromisoformat(clean_date(p["date"]))
            when = email.utils.format_datetime(datetime.datetime(d.year, d.month, d.day, 12, 0, tzinfo=datetime.timezone.utc))
            cats = "".join("<category>%s</category>\n" % x(t) for t in p.get("tags", []) if slugify(t))
            items.append(
                "<item>\n<title>%s</title>\n<link>%s</link>\n<guid isPermaLink=\"true\">%s</guid>\n"
                "<pubDate>%s</pubDate>\n%s<description>%s</description>\n"
                "<content:encoded><![CDATA[%s]]></content:encoded>\n</item>"
                % (x(p["title"]), x(link), x(link), when, cats, x(summary), body.replace("]]>", "]]]]><![CDATA[>"))
            )
        now = email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/">\n'
            "<channel>\n<title>%s</title>\n<link>%s/</link>\n<description>%s</description>\n<language>%s</language>\n"
            "<lastBuildDate>%s</lastBuildDate>\n<generator>SiteGen</generator>\n"
            '<atom:link href="%s/rss" rel="self" type="application/rss+xml"/>\n%s\n</channel>\n</rss>\n'
            % (x(site["title"]), x(base), x(site.get("tagline") or site["title"]), x(site.get("lang") or "en"),
               now, x(base), "\n".join(items))
        )

    def write_rss(self):
        site = self.cfg["site"]
        paths = [self.root / "rss", self.root / "rss.xml"]
        if site.get("rss") and site.get("url"):
            xml = self.rss_xml(self.cfg)
            for f in paths:
                if not f.is_dir():
                    write_text(f, xml)
        else:
            for f in paths:
                if f.is_file() and "<generator>SiteGen</generator>" in read_text(f)[:2000]:
                    f.unlink()

    def sitemap_xml(self):
        c = self.cfg
        site = c["site"]
        url = site["url"].rstrip("/")
        rows = []

        def add(rel, lastmod=""):
            loc = _escape(url + "/" + rel.lstrip("/"), quote=False)
            lm = "<lastmod>%s</lastmod>" % lastmod if lastmod else ""
            rows.append("<url><loc>%s</loc>%s</url>" % (loc, lm))

        for p in c["pages"]:
            if not p.get("noindex"):
                add(page_file(p))
        for post in published_posts(c):
            if not post.get("noindex"):
                add(post_file(post["slug"]), clean_date(post.get("date")))
        for slug in tag_map(c):
            add("blog/tag/%s.html" % slug)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n<!-- generated by SiteGen -->\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n%s\n</urlset>\n' % "\n".join(rows)
        )

    def write_seo_files(self):
        """Generate sitemap.xml and robots.txt when the site URL and the relevant toggles are set.
        Files SiteGen did not create are left untouched, so a hand-written robots.txt is never overwritten."""
        site = self.cfg["site"]
        url = (site.get("url") or "").strip().rstrip("/")
        noindex = bool(site.get("noindex"))
        sm, rb = self.root / "sitemap.xml", self.root / "robots.txt"

        def is_ours(path, marker):
            return path.is_file() and marker in read_text(path)[:300]

        want_sitemap = bool(site.get("sitemap")) and bool(url) and not noindex
        if want_sitemap and not sm.is_dir():
            write_text(sm, self.sitemap_xml())
        elif not want_sitemap and is_ours(sm, "generated by SiteGen"):
            sm.unlink()

        if url and noindex:
            robots = "# generated by SiteGen\nUser-agent: *\nDisallow: /\n"
        elif want_sitemap:
            robots = "# generated by SiteGen\nUser-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % url
        else:
            robots = None
        if robots is not None:
            if not rb.is_dir() and (not rb.exists() or is_ours(rb, "generated by SiteGen")):
                write_text(rb, robots)
        elif is_ours(rb, "generated by SiteGen"):
            rb.unlink()

    def build(self):
        c = self.cfg
        for p in c["pages"]:
            write_text(self.root / page_file(p), self.page_html(c, p, read_text(self.page_md(p["slug"]))))
        for post in c["posts"]:
            if post.get("status") == "published":
                write_text(self.root / post_file(post["slug"]), self.post_html(c, post, read_text(self.p_post(post["slug"]))))
        tags = tag_map(c)
        tag_dir = self.root / "blog" / "tag"
        for slug, info in tags.items():
            write_text(tag_dir / (slug + ".html"), self.tag_html(c, info["name"], info["posts"]))
        if tag_dir.is_dir():
            for f in tag_dir.glob("*.html"):
                if f.stem not in tags:
                    f.unlink()
            try:
                tag_dir.rmdir()
            except OSError:
                pass
        self.write_rss()
        self.write_seo_files()

    # ---- pages
    def unique_page_slug(self, base):
        base = slugify(base) or "page"
        taken = {p["slug"] for p in self.cfg["pages"]} | RESERVED_SLUGS
        slug, n = base, 2
        while slug in taken:
            slug = "%s-%d" % (base, n)
            n += 1
        return slug

    def add_css_stub(self, slug, title):
        css = read_text(self.css_path())
        if ("body.page-%s" % slug) not in css:
            write_text(self.css_path(), css.rstrip("\n") + "\n" + css_stub(slug, title))

    def add_page(self, title, kind):
        c = self.cfg
        if kind in ("about", "privacy", "blog"):
            existing = next((p for p in c["pages"] if p["kind"] == kind), None)
            if existing:
                return existing
        title = (title or "").strip()[:120]
        site = c["site"]
        if kind == "about":
            title, slug = title or "About", self.unique_page_slug("about")
            md = ABOUT_MD.replace("[your name]", site.get("author") or "[your name]")
        elif kind == "privacy":
            title, slug = title or "Privacy policy", self.unique_page_slug("privacy")
            md = PRIVACY_MD.replace("[DATE]", nice_date(datetime.date.today().isoformat())).replace("[SITE]", site["title"])
        elif kind == "blog":
            title, slug, md = title or "Blog", "blog", ""
        else:
            kind = "page"
            title = title or "Untitled page"
            slug, md = self.unique_page_slug(title), ""
        page = {"slug": slug, "title": title, "kind": kind, "show_title": True}
        c["pages"].append(page)
        c["nav"].append({"type": "page", "slug": slug, "label": title, "visible": True})
        write_text(self.page_md(slug), md)
        self.add_css_stub(slug, title)
        self.save_cfg()
        self.build()
        return page

    def get_page(self, slug):
        p = find_page(self.cfg, slug)
        if not p:
            raise ApiError("That page does not exist.", 404)
        return {"page": p, "markdown": read_text(self.page_md(slug)), "file": page_file(p)}

    def save_page(self, slug, d):
        p = find_page(self.cfg, slug)
        if not p:
            raise ApiError("That page does not exist.", 404)
        old = p["title"]
        title = str(d.get("title") or "").strip()[:120] or old
        p["title"] = title
        p["show_title"] = bool(d.get("show_title", True))
        apply_seo(p, d)
        for n in self.cfg["nav"]:
            if n.get("type") == "page" and n.get("slug") == slug and n.get("label") == old:
                n["label"] = title
        write_text(self.page_md(slug), str(d.get("markdown") or ""))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def delete_page(self, slug):
        p = find_page(self.cfg, slug)
        if not p:
            raise ApiError("That page does not exist.", 404)
        if p["kind"] == "home":
            raise ApiError("The home page cannot be deleted.")
        self.cfg["pages"].remove(p)
        self.cfg["nav"] = [n for n in self.cfg["nav"] if not (n.get("type") == "page" and n.get("slug") == slug)]
        (self.root / page_file(p)).unlink(missing_ok=True)
        self.page_md(slug).unlink(missing_ok=True)
        self.save_cfg()
        self.build()
        return {"ok": True}

    def ensure_feed(self):
        if any(p["kind"] == "blog" for p in self.cfg["pages"]):
            return False
        self.add_page("Blog", "blog")
        return True

    # ---- posts
    def find_post(self, slug):
        return next((p for p in self.cfg["posts"] if p["slug"] == slug), None)

    def unique_post_slug(self, base, exclude=None):
        base = slugify(str(base)) or "post"
        taken = {p["slug"] for p in self.cfg["posts"] if p["slug"] != exclude}
        slug, n = base, 2
        while slug in taken:
            slug = "%s-%d" % (base, n)
            n += 1
        return slug

    def get_post(self, slug):
        p = self.find_post(slug)
        if not p:
            raise ApiError("That post does not exist.", 404)
        path = self.p_post(slug) if p["status"] == "published" else self.p_draft(slug)
        return {"post": p, "markdown": read_text(path)}

    def save_post(self, d):
        c = self.cfg
        title = str(d.get("title") or "").strip()[:200]
        publish = bool(d.get("publish"))
        if publish and not title:
            raise ApiError("Add a title before publishing.")
        title = title or "Untitled post"
        md = str(d.get("markdown") or "")
        date = clean_date(d.get("date"))
        summary = str(d.get("summary") or "").strip()[:400]
        known = {slugify(t): t for q in c["posts"] for t in q.get("tags", []) if slugify(t)}
        tags = clean_tags(d.get("tags"), strict=True, known=known)
        old = str(d.get("slug") or "")
        post = self.find_post(old) if old else None
        if old and post is None:
            raise ApiError("That post no longer exists.", 404)
        was_published = bool(post and post["status"] == "published")
        now = datetime.datetime.now().isoformat(timespec="seconds")
        feed_created = False

        if post is None:
            slug = self.unique_post_slug(d.get("new_slug") or title)
            post = {"slug": slug, "status": "draft", "created_at": now, "published_at": ""}
            c["posts"].append(post)
        elif post["status"] == "draft":
            wanted = slugify(str(d.get("new_slug") or "")) or slugify(title) or "post"
            if wanted != post["slug"]:
                new_slug = self.unique_post_slug(wanted, exclude=post["slug"])
                self.p_draft(post["slug"]).unlink(missing_ok=True)
                post["slug"] = new_slug
        post.update(title=title, date=date, summary=summary, tags=tags)
        apply_seo(post, d)
        slug = post["slug"]

        if publish and not was_published:
            post["status"] = "published"
            post["published_at"] = now
            self.p_draft(slug).unlink(missing_ok=True)
            feed_created = self.ensure_feed()
        if post["status"] == "published":
            write_text(self.p_post(slug), md)
        else:
            write_text(self.p_draft(slug), md)
        self.save_cfg()
        self.build()
        return {"slug": slug, "post": post, "feed_created": feed_created, "was_published": was_published}

    def unpublish_post(self, slug):
        p = self.find_post(slug)
        if not p:
            raise ApiError("That post does not exist.", 404)
        if p["status"] == "published":
            write_text(self.p_draft(slug), read_text(self.p_post(slug)))
            self.p_post(slug).unlink(missing_ok=True)
            (self.root / post_file(slug)).unlink(missing_ok=True)
            p["status"] = "draft"
            self.save_cfg()
            self.build()
        return {"ok": True}

    def delete_post(self, slug):
        p = self.find_post(slug)
        if not p:
            raise ApiError("That post does not exist.", 404)
        self.cfg["posts"].remove(p)
        self.p_post(slug).unlink(missing_ok=True)
        self.p_draft(slug).unlink(missing_ok=True)
        (self.root / post_file(slug)).unlink(missing_ok=True)
        self.save_cfg()
        self.build()
        return {"ok": True}

    # ---- css, menu, settings, uploads
    def get_css(self):
        if not self.css_path().exists():
            write_text(self.css_path(), default_css("clean"))
        return {"css": read_text(self.css_path())}

    def save_css(self, text):
        write_text(self.css_path(), str(text))
        return {"ok": True}

    def clean_nav(self, nav, cfg):
        slugs = {p["slug"] for p in cfg["pages"]}
        out = []
        for n in nav if isinstance(nav, list) else []:
            if not isinstance(n, dict):
                continue
            vis = bool(n.get("visible", True))
            label = str(n.get("label", ""))[:80]
            if n.get("type") == "page" and n.get("slug") in slugs:
                out.append({"type": "page", "slug": n["slug"], "label": label, "visible": vis})
            elif n.get("type") == "link":
                out.append({"type": "link", "label": label, "url": str(n.get("url", ""))[:500], "visible": vis})
        return out

    def save_nav(self, nav):
        self.cfg["nav"] = self.clean_nav(nav, self.cfg)
        self.normalize()
        self.save_cfg()
        self.build()
        return {"ok": True}

    @staticmethod
    def clean_site(d, current):
        out = dict(current)
        for k, limit in (("title", 120), ("tagline", 200), ("footer", 400), ("author", 120), ("lang", 12)):
            if k in d:
                out[k] = str(d[k]).strip()[:limit]
        if "url" in d:
            out["url"] = str(d["url"]).strip().rstrip("/")[:300]
        if "rss" in d:
            out["rss"] = bool(d["rss"])
        if "description" in d:
            out["description"] = str(d["description"]).strip()[:320]
        if "image" in d:
            out["image"] = str(d["image"]).strip()[:400]
        if "twitter" in d:
            out["twitter"] = str(d["twitter"]).strip()[:60]
        if "noindex" in d:
            out["noindex"] = bool(d["noindex"])
        if "sitemap" in d:
            out["sitemap"] = bool(d["sitemap"])
        out["title"] = out.get("title") or current.get("title") or "My site"
        out["lang"] = out.get("lang") or "en"
        return out

    def save_settings(self, d):
        site = self.clean_site(d, self.cfg["site"])
        url = site.get("url", "")
        if url and not re.match(r"^https?://[^\s/]+", url):
            raise ApiError("The site URL must start with http:// or https://, for example https://example.com")
        if site.get("rss"):
            if not url:
                raise ApiError("Enter your site URL to turn on the RSS feed. Feed readers need full web addresses.")
            if (self.root / "rss").is_dir():
                raise ApiError("A folder named rss already exists in the site folder. Rename or remove it to publish the feed.")
        if site.get("sitemap"):
            if not url:
                raise ApiError("Enter your site URL to generate a sitemap. It needs full web addresses.")
            if (self.root / "sitemap.xml").is_dir():
                raise ApiError("A folder named sitemap.xml already exists in the site folder. Rename or remove it to generate the sitemap.")
        self.cfg["site"] = site
        self.save_cfg()
        self.build()
        return {"ok": True}

    def upload(self, name, data):
        stem, ext = os.path.splitext(os.path.basename(name))
        ext = ext.lower()
        if ext not in IMG_EXT:
            raise ApiError("Only image files can be added (png, jpg, gif, webp, svg, avif).")
        if not data:
            raise ApiError("That file is empty.")
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-") or "image"
        folder = self.root / "assets"
        folder.mkdir(exist_ok=True)
        fn, n = stem + ext, 2
        while (folder / fn).exists():
            fn = "%s-%d%s" % (stem, n, ext)
            n += 1
        (folder / fn).write_bytes(data)
        return {"path": "assets/" + fn}

    # ---- preview
    def preview(self, p):
        cfg = copy.deepcopy(self.cfg)
        if isinstance(p.get("site"), dict):
            cfg["site"] = self.clean_site(p["site"], cfg["site"])
        if p.get("nav") is not None:
            cfg["nav"] = self.clean_nav(p["nav"], cfg)
        css = p.get("css") if isinstance(p.get("css"), str) else None
        if css is None:
            css = read_text(self.css_path())  # inlined so the preview never needs a stylesheet request
        view = p.get("view")

        seo_fields = {
            "seo_title": str(p.get("seo_title") or ""),
            "description": str(p.get("description") or ""),
            "image": str(p.get("image") or ""),
            "canonical": str(p.get("canonical") or ""),
            "noindex": bool(p.get("noindex")),
        }
        if view == "post":
            post = dict(
                slug=slugify(str(p.get("slug") or "")) or "post",
                title=str(p.get("title") or "").strip() or "Untitled post",
                date=clean_date(p.get("date")),
                summary=str(p.get("summary") or ""),
                tags=clean_tags(p.get("tags")),
                **seo_fields,
            )
            html_, seo = self.render_post(cfg, post, str(p.get("markdown") or ""), css=css, base="/site/blog/posts/")
            return {"html": html_, "seo": seo}

        if view == "page":
            page = find_page(cfg, str(p.get("slug") or ""))
            if not page:
                raise ApiError("That page does not exist.", 404)
            page["title"] = str(p.get("title") or "").strip() or page["title"]
            page["show_title"] = bool(p.get("show_title", True))
            page.update(seo_fields)
            md = str(p.get("markdown") or "")
            base = "/site/blog/" if page["kind"] == "blog" else "/site/"
            html_, seo = self.render_page(cfg, page, md, css=css, base=base)
            return {"html": html_, "seo": seo}
        else:
            target = str(p.get("target") or "page:index")
            kind, _, slug = target.partition(":")
            if kind == "post":
                post = next((x for x in cfg["posts"] if x["slug"] == slug), None)
                if post:
                    html_ = self.post_html(cfg, post, self.read_post_md(slug), css=css, base="/site/blog/posts/")
                    return {"html": html_}
            page = find_page(cfg, slug) or find_page(cfg, "index")
            md = read_text(self.page_md(page["slug"]))
            base = "/site/" + ("blog/" if page["kind"] == "blog" else "")
            html_, seo = self.render_page(cfg, page, md, css=css, base=base)
            return {"html": html_, "seo": seo}


# ----------------------------------------------------------------------------
# Opening sites, remembering the last one
# ----------------------------------------------------------------------------

def remember_folder(path):
    try:
        write_text(CONFIG_HOME / "last.json", json.dumps({"folder": str(path)}))
    except OSError:
        pass


def last_folder():
    try:
        return json.loads((CONFIG_HOME / "last.json").read_text(encoding="utf-8")).get("folder")
    except Exception:
        return None


def open_site(path, confirm=False):
    path = str(path or "").strip()
    if not path:
        raise ApiError("Enter a folder path.")
    root = Path(path).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ApiError("That path is a file, not a folder.")
    is_site = (root / META_DIR / "config.json").exists()
    if not is_site and root.exists() and not confirm:
        if any(not n.name.startswith(".") for n in root.iterdir()):
            return {
                "needs_confirm": True,
                "message": (
                    "This folder already has files that SiteGen did not create.\n\n"
                    "SiteGen will add its own files here and will overwrite index.html and any page "
                    "it generates with the same name. Existing style.css and images are kept.\n\n"
                    "Use this folder anyway?"
                ),
            }
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ApiError("Could not create that folder: %s" % e)
    site = Site(root)
    site.load()
    CURRENT["site"] = site
    remember_folder(root)
    return {"state": site.state()}


def app_state():
    site = CURRENT["site"]
    if site is None:
        return {"folder": None, "suggest": str(Path.home() / "my-site"), "last": last_folder()}
    return site.state()


def pick_folder():
    site = CURRENT["site"]
    start = str(site.root.parent) if site else str(Path.home())
    cmds = []
    if shutil.which("kdialog"):
        cmds.append(["kdialog", "--getexistingdirectory", start])
    if shutil.which("zenity"):
        cmds.append(["zenity", "--file-selection", "--directory", "--filename=" + start.rstrip("/") + "/"])
    if sys.platform == "darwin":
        cmds.append(["osascript", "-e", "POSIX path of (choose folder)"])
    cmds.append([sys.executable, "-c",
                 "import tkinter as t, tkinter.filedialog as f; r=t.Tk(); r.withdraw(); print(f.askdirectory())"])
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except Exception:
            continue
        if r.returncode == 0:
            return {"path": r.stdout.strip() or None, "available": True}
        if r.returncode == 1 and cmd[0] != sys.executable:
            return {"path": None, "available": True}
    return {"path": None, "available": False}


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

def api(method, route, query, h):
    q = parse_qs(query)
    parts = [unquote(x) for x in route.strip("/").split("/")]

    if parts == ["state"] and method == "GET":
        return app_state()
    if parts == ["open"] and method == "POST":
        d = h.read_json()
        return open_site(d.get("path", ""), bool(d.get("confirm")))

    site = CURRENT["site"]
    if site is None:
        raise ApiError("Open a site folder first.", 409)

    if parts == ["pages"] and method == "POST":
        d = h.read_json()
        return {"slug": site.add_page(d.get("title", ""), d.get("kind", "page"))["slug"]}
    if len(parts) == 2 and parts[0] == "pages":
        if method == "GET":
            return site.get_page(parts[1])
        if method == "PUT":
            return site.save_page(parts[1], h.read_json())
        if method == "DELETE":
            return site.delete_page(parts[1])
    if parts == ["posts"] and method == "POST":
        return site.save_post(h.read_json())
    if len(parts) == 2 and parts[0] == "posts":
        if method == "GET":
            return site.get_post(parts[1])
        if method == "DELETE":
            return site.delete_post(parts[1])
    if len(parts) == 3 and parts[0] == "posts" and parts[2] == "unpublish" and method == "POST":
        return site.unpublish_post(parts[1])
    if parts == ["css"]:
        if method == "GET":
            return site.get_css()
        if method == "PUT":
            return site.save_css(h.read_json().get("css", ""))
    if parts == ["nav"] and method == "PUT":
        return site.save_nav(h.read_json().get("nav", []))
    if parts == ["settings"] and method == "PUT":
        return site.save_settings(h.read_json())
    if parts == ["preview"] and method == "POST":
        return site.preview(h.read_json())
    if parts == ["upload"] and method == "POST":
        return site.upload(q.get("name", ["image"])[0], h.read_body(MAX_UPLOAD))
    if parts == ["build"] and method == "POST":
        site.build()
        return {"ok": True}
    raise ApiError("Unknown request.", 404)


class Handler(BaseHTTPRequestHandler):
    server_version = "SiteGen"

    def log_message(self, *args):
        pass

    # ---- helpers
    def read_body(self, limit):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ApiError("That file is too large.", 413)
        return self.rfile.read(n) if n else b""

    def read_json(self):
        raw = self.read_body(8 * 1024 * 1024)
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            raise ApiError("Bad request body.")

    def send_bytes(self, status, ctype, data):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_bytes(status, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8"))

    def send_text(self, status, text):
        self.send_bytes(status, "text/plain; charset=utf-8", text.encode("utf-8"))

    def host_ok(self):
        port = SERVER["port"]
        return self.headers.get("Host", "") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def send_site(self, rel):
        site = CURRENT["site"]
        if site is None:
            return self.send_text(404, "No site is open.")
        rel = unquote(rel)
        if "\x00" in rel:
            return self.send_text(404, "Not found")
        target = (site.root / rel).resolve()
        try:
            parts = target.relative_to(site.root).parts
        except ValueError:
            return self.send_text(403, "Forbidden")
        if any(p.startswith(".") for p in parts):
            return self.send_text(404, "Not found")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return self.send_text(404, "Not found. Save the page first so it gets generated.")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.name == "rss":
            ctype = "application/rss+xml"
        if ctype.startswith("text/") or ctype.endswith("xml") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self.send_bytes(200, ctype, target.read_bytes())

    # ---- routing
    def dispatch(self, method):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            if not self.host_ok():
                return self.send_text(403, "Forbidden")
            if method == "GET" and path == "/":
                return self.send_bytes(200, "text/html; charset=utf-8", UI_HTML.replace("__TOKEN__", TOKEN).encode("utf-8"))
            if method == "GET" and path.startswith("/site/"):
                return self.send_site(path[len("/site/"):])
            if path.startswith("/api/"):
                if self.headers.get("X-Token") != TOKEN:
                    return self.send_json({"error": "Not authorized."}, 401)
                route = path[len("/api/"):]
                if route == "pick-folder" and method == "POST":
                    return self.send_json(pick_folder())
                if route == "quit" and method == "POST":
                    self.send_json({"ok": True})
                    threading.Thread(target=SERVER["httpd"].shutdown, daemon=True).start()
                    return
                with LOCK:
                    result = api(method, route, parts.query, self)
                return self.send_json(result)
            return self.send_text(404, "Not found")
        except ApiError as e:
            self.send_json({"error": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.send_json({"error": "Unexpected error: %s" % e}, 500)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")


# ----------------------------------------------------------------------------
# Editor UI (served to your browser; this part uses JavaScript, generated sites do not)
# ----------------------------------------------------------------------------

UI_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SiteGen</title>
<style>
:root {
  color-scheme: light;
  --ink: #1a2130; --ink2: #252e42; --paper: #eef1f6; --panel: #ffffff; --panel2: #f7f9fc; --field: #ffffff;
  --line: #d3d9e3; --text: #1c2432; --muted: #5b6678; --accent: #2c5fe6;
  --primary: #2c5fe6; --primary-d: #2249b8;
  --btn-bg: #ffffff; --btn-hover: #f7f9fc; --btn-line-hover: #aab3c2;
  --side: #e5e9f0; --side-hover: #d9dfe9; --side-active: #ffffff;
  --right: #dde2ea; --right-bar: #eef1f6;
  --danger: #b3261e; --amber: #8a5a00; --amber-bg: #fff8e6; --amber-line: #e2c98f;
  --ok: #1c7a45; --ok-bg: #eefaf3; --ok-line: #a9d9bd;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --ink: #0b0f19; --ink2: #1a2233; --paper: #0f1420; --panel: #161c2a; --panel2: #1b2233; --field: #0f1522;
  --line: #2c3548; --text: #e2e8f4; --muted: #98a3b8; --accent: #7ea2ff;
  --primary: #3d68e6; --primary-d: #5079ee;
  --btn-bg: #1f2740; --btn-hover: #263050; --btn-line-hover: #4a5878;
  --side: #121826; --side-hover: #1d2536; --side-active: #1f2940;
  --right: #0c111b; --right-bar: #131a28;
  --danger: #ff8a80; --amber: #f0c060; --amber-bg: #2b2412; --amber-line: #5a4a1e;
  --ok: #5fd69a; --ok-bg: #12281c; --ok-line: #24593a;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { height: 100%; margin: 0; }
body { font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--text); background: var(--paper); }
button, input, select, textarea { font: inherit; color: inherit; }
button { border: 1px solid var(--line); background: var(--btn-bg); padding: 5px 11px; border-radius: 6px; cursor: pointer; }
button:hover { border-color: var(--btn-line-hover); background: var(--btn-hover); }
button.primary { background: var(--primary); border-color: var(--primary); color: #fff; }
button.primary:hover { background: var(--primary-d); border-color: var(--primary-d); }
button.danger { color: var(--danger); }
input[type=text], input[type=date], input:not([type]), input[type=url], select {
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; background: var(--field); min-width: 0;
}
:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.grow { flex: 1; }
.hint { color: var(--muted); font-size: 12px; }

#workspace { display: flex; flex-direction: column; height: 100vh; }
#top { background: var(--ink); border-bottom: 1px solid var(--ink2); color: #fff; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 8px 12px; }
#top button { padding: 4px 9px; background: transparent; border-color: #3b465e; color: #e8ecf5; }
#top button:hover { background: var(--ink2); border-color: #5a6785; }
#top .brand { font-weight: 700; margin-right: 6px; letter-spacing: 0.01em; }
#top .sep { width: 1px; height: 22px; background: #3b465e; margin: 0 6px; }
#folder { color: #9aa6bf; font: 12px ui-monospace, Consolas, monospace; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#dirty { color: #ffd479; font-size: 12px; }

#main { flex: 1; min-height: 0; display: grid; grid-template-columns: 220px minmax(0, 1fr) minmax(0, 1fr); }
#side { background: var(--side); border-right: 1px solid var(--line); overflow: auto; padding: 8px; }
#side h3 { font-size: 12px; font-weight: 600; color: var(--muted); margin: 14px 6px 4px; }
#side h3:first-child { margin-top: 4px; }
.item { display: flex; justify-content: space-between; align-items: center; gap: 6px; width: 100%; text-align: left; border: 0; background: transparent; padding: 6px 8px; border-radius: 6px; }
.item:hover { background: var(--side-hover); }
.item.active { background: var(--side-active); box-shadow: inset 3px 0 0 var(--accent); }
.item .t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip { font-size: 11px; padding: 1px 7px; border-radius: 99px; border: 1px solid var(--line); white-space: nowrap; }
.chip.draft { color: var(--amber); border-color: var(--amber-line); background: var(--amber-bg); }
.chip.pub { color: var(--ok); border-color: var(--ok-line); background: var(--ok-bg); }
.empty { color: var(--muted); padding: 4px 8px; font-size: 13px; }

#left { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--panel); border-right: 1px solid var(--line); overflow: auto; }
#left .fill { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 10px; border-bottom: 1px solid var(--line); }
.bar.sub { background: var(--panel2); }
.bar label { display: inline-flex; align-items: center; gap: 6px; }
.bar label.wide { flex: 1; min-width: 220px; }
.bar label.wide input { flex: 1; }
.title-input { flex: 1; min-width: 140px; font-size: 16px; font-weight: 600; border: 1px solid transparent !important; padding: 5px 8px; }
.title-input:hover, .title-input:focus { border-color: var(--line) !important; }
.md-tools { display: flex; flex-wrap: wrap; gap: 4px; padding: 6px 10px; border-bottom: 1px solid var(--line); }
.md-tools button { padding: 2px 9px; font-size: 13px; }
.code { flex: 1; width: 100%; min-height: 240px; border: 0; resize: none; padding: 14px; background: var(--panel); color: var(--text);
  font: 14px/20px ui-monospace, "SF Mono", Consolas, "DejaVu Sans Mono", monospace; tab-size: 2; }
.slug-wrap { display: inline-flex; align-items: center; gap: 2px; color: var(--muted); }
.slug-wrap input { width: 150px; }
.pad { padding: 14px; display: flex; flex-direction: column; gap: 12px; max-width: 560px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field input { width: 100%; }
.nav-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 10px; border-bottom: 1px solid var(--line); }
.nav-row.off .n-label { opacity: 0.55; }
.nav-row .n-label { width: 150px; }
.nav-row .n-url { flex: 1; min-width: 160px; }
.nav-row .n-target { color: var(--muted); font: 12px ui-monospace, Consolas, monospace; }

#right { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--right); }
#right .bar { background: var(--right-bar); }
#right a { color: var(--accent); }
#preview { flex: 1; width: 100%; border: 0; background: #fff; }
#toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: var(--ink); color: #fff; padding: 8px 16px; border-radius: 8px; opacity: 0; pointer-events: none; transition: opacity 0.15s; }
#toast.show { opacity: 1; }
#toast.bad { background: #b3261e; }
.tagpick { display: inline-flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.tagpick button { padding: 1px 9px; font-size: 12px; border-radius: 99px; }
.tagpick button.on { background: var(--primary); border-color: var(--primary); color: #fff; }
details.seo { border-top: 1px solid var(--line); background: var(--panel2); }
details.seo > summary { cursor: pointer; padding: 8px 10px; font-weight: 600; list-style: none; display: flex; align-items: center; gap: 8px; }
details.seo > summary::-webkit-details-marker { display: none; }
details.seo > summary::before { content: "\25B8"; color: var(--muted); font-size: 12px; }
details.seo[open] > summary::before { content: "\25BE"; }
details.seo .seo-note { font-weight: 400; color: var(--muted); font-size: 12px; }
.seo-grid { padding: 4px 10px 12px; display: flex; flex-direction: column; gap: 10px; max-width: 620px; }
.seo-grid .field textarea { border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; background: var(--field); color: var(--text); resize: vertical; font: inherit; }
.hint.warn { color: var(--amber); }
.snippet { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; background: var(--panel); }
.snip-url { color: var(--ok); font-size: 12px; word-break: break-all; }
.snip-title { color: #1a4fd0; font-size: 17px; line-height: 1.3; margin: 2px 0; }
:root[data-theme="dark"] .snip-title { color: #9bb8ff; }
.snip-desc { color: var(--muted); font-size: 13px; }
.snip-flag { margin-top: 6px; color: var(--amber); font-size: 12px; }

#welcome { max-width: 520px; margin: 12vh auto; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 28px; }
#welcome h1 { margin: 0 0 6px; font-size: 22px; }
#welcome p { color: var(--muted); margin: 0 0 18px; }
#welcome .row { display: flex; gap: 8px; margin-bottom: 12px; }
#welcome .row input { flex: 1; }
</style>
<script>
(function () {
  var t = null;
  try { t = localStorage.getItem("sitegen-theme"); } catch (e) { t = null; }
  if (t !== "dark" && t !== "light") t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", t);
})();
</script>
</head>
<body>

<div id="welcome" hidden>
  <h1>SiteGen</h1>
  <p>Choose the folder where your site lives. If the folder is new or empty, a fresh site is created there. If it holds a SiteGen site, it opens as you left it.</p>
  <div class="row">
    <input id="w-path" aria-label="Site folder path" placeholder="/home/you/my-site">
    <button id="w-browse">Browse</button>
  </div>
  <button id="w-open" class="primary">Open site</button>
</div>

<div id="workspace" hidden>
  <div id="top">
    <span class="brand">SiteGen</span>
    <button data-act="add-page">Add page</button>
    <button data-act="blog" id="b-blog">Define blog feed</button>
    <button data-act="about" id="b-about">Define about page</button>
    <button data-act="privacy" id="b-privacy">Define privacy policy</button>
    <span class="sep"></span>
    <button data-act="add-post">Add blog post</button>
    <span class="sep"></span>
    <button data-act="css">Edit CSS</button>
    <button data-act="menu">Edit menu</button>
    <button data-act="settings">Site settings</button>
    <span class="grow"></span>
    <span id="dirty" hidden>Unsaved changes</span>
    <span id="folder" title=""></span>
    <button data-act="theme" id="b-theme">Dark mode</button>
    <button data-act="switch">Switch folder</button>
    <button data-act="quit">Quit</button>
  </div>
  <div id="main">
    <aside id="side" aria-label="Pages and posts"></aside>
    <section id="left" aria-label="Editor"></section>
    <section id="right" aria-label="Preview">
      <div class="bar"><span class="hint">Live preview</span><span class="grow"></span><a id="open-built" href="/site/index.html" target="_blank" rel="noopener">Open built site in a tab</a></div>
      <iframe id="preview" title="Site preview" sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"></iframe>
    </section>
  </div>
</div>
<div id="toast" role="status"></div>

<script>
const TOKEN = "__TOKEN__";
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

let S = null;
let view = { type: "none" };
let dirty = false;
let navDraft = [];
let timer = null;

const PAGE_TPL = `
<div class="fill">
  <div class="bar">
    <input id="p-title" class="title-input" placeholder="Page title" aria-label="Page title">
    <button id="p-save" class="primary">Save</button>
  </div>
  <div class="bar sub">
    <label><input type="checkbox" id="p-show"> Show the title at the top of the page</label>
    <span class="grow"></span>
    <span class="hint" id="p-file"></span>
    <button id="p-delete" class="danger">Delete page</button>
  </div>
  SEOBLOCK
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Write in Markdown"></textarea>
</div>`;

const MD_TOOLS = `
<div class="md-tools">
  <button data-md="bold"><b>Bold</b></button>
  <button data-md="italic"><i>Italic</i></button>
  <button data-md="heading">Heading</button>
  <button data-md="link">Link</button>
  <button data-md="image">Image</button>
  <button data-md="code">Code</button>
  <button data-md="list">List</button>
  <button data-md="quote">Quote</button>
  <span class="hint">Links and images use paths from the site root, for example about.html or assets/photo.png</span>
  <input type="file" id="img-file" accept="image/*" hidden>
</div>`;

const POST_TPL = `
<div class="fill">
  <div class="bar">
    <input id="t-title" class="title-input" placeholder="Post title" aria-label="Post title">
    <span id="t-status" class="chip"></span>
    <button id="t-save">Save</button>
    <button id="t-publish" class="primary">Publish</button>
    <button id="t-unpublish">Unpublish</button>
  </div>
  <div class="bar sub">
    <label>Date <input type="date" id="t-date"></label>
    <label>Address <span class="slug-wrap">blog/posts/<input id="t-slug" aria-label="Post address"></span>.html</label>
    <label class="wide">Summary <input id="t-summary" placeholder="Optional. Defaults to the first paragraph."></label>
    <button id="t-delete" class="danger">Delete</button>
  </div>
  <div class="bar sub">
    <label class="wide">Tags <input id="t-tags" placeholder="Life, Tech (separate with commas)"></label>
    <span class="tagpick" id="t-tagpick" aria-label="Existing tags"></span>
  </div>
  SEOBLOCK
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Write your post in Markdown"></textarea>
</div>`;

const CSS_TPL = `
<div class="fill">
  <div class="bar">
    <label>Theme <select id="theme"></select></label>
    <button id="apply-theme">Apply theme</button>
    <span class="grow"></span>
    <button id="css-save" class="primary">Save CSS</button>
  </div>
  <div class="bar sub">
    <label>Preview <select id="css-target"></select></label>
    <span class="grow"></span>
    <button id="jump-global">Go to global</button>
    <button id="jump-pages">Go to page specific</button>
  </div>
  <textarea id="css" class="code" spellcheck="false" wrap="off"></textarea>
</div>`;

const MENU_TPL = `
<div class="fill">
  <div class="bar">
    <strong>Menu</strong>
    <span class="hint">Untick a page to hide it from the menu. The page itself stays.</span>
    <span class="grow"></span>
    <button id="nav-save" class="primary">Save menu</button>
  </div>
  <div id="nav-rows"></div>
  <div class="bar sub">
    <strong>Add a custom link</strong>
    <input id="nl-label" placeholder="Label" aria-label="Link label" style="width:130px">
    <input id="nl-url" placeholder="https://example.com" aria-label="Link address" style="flex:1;min-width:160px">
    <button id="nl-add">Add link</button>
  </div>
</div>`;

const SET_TPL = `
<div class="fill">
  <div class="bar"><strong>Site settings</strong><span class="grow"></span><button id="s-save" class="primary">Save settings</button></div>
  <div class="pad">
    <label class="field">Site title <input id="s-title"></label>
    <label class="field">Tagline <input id="s-tagline" placeholder="Shown under the site title"></label>
    <label class="field">Meta description <textarea id="s-description" rows="2" maxlength="320" placeholder="Default description for search results and social cards. Pages and posts can override it."></textarea><span class="hint" id="s-description-count"></span></label>
    <label class="field">Footer text <input id="s-footer"><span class="hint">You can use {year} and {title}. Inline markdown works.</span></label>
    <label class="field">Author name <input id="s-author"></label>
    <label class="field">Site URL <input id="s-url" placeholder="https://example.com"><span class="hint">Used for the RSS feed, canonical links, the sitemap and social tags. All of these need full web addresses. No trailing slash needed.</span></label>
    <label><input type="checkbox" id="s-rss"> Publish an RSS feed at <b id="s-rss-addr"></b></label>
    <label><input type="checkbox" id="s-sitemap"> Generate a sitemap at <b id="s-sitemap-addr"></b> and a matching robots.txt</label>
    <label class="field">Default social image <input id="s-image" placeholder="assets/cover.jpg or a full URL"><span class="hint">Shown when a page is shared and no page or post sets its own image. Relative paths need the site URL.</span></label>
    <label class="field">Twitter / X handle <input id="s-twitter" placeholder="@yourhandle" style="max-width:220px"></label>
    <label><input type="checkbox" id="s-noindex"> Ask search engines not to index this site<span class="hint" style="display:block">Adds a noindex tag to every page and makes robots.txt disallow crawling. Use for a staging site.</span></label>
    <label class="field">Language code <input id="s-lang" placeholder="en" style="max-width:100px"></label>
  </div>
</div>`;

async function api(method, path, body) {
  const opt = { method, headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch("/api/" + path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { data = { error: "Unexpected response from the server." }; }
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toast(msg, kind) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show " + (kind || "");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { t.className = ""; }, 2800);
}

function setDirty(v) { dirty = v; $("#dirty").hidden = !v; }
function markDirty() { setDirty(true); schedulePreview(); }
async function guard() { return !dirty || window.confirm("You have unsaved changes. Discard them?"); }
function slugify(s) {
  return s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}
function today() { return new Date().toLocaleDateString("en-CA"); }

/* ---------- SEO block ---------- */
function seoBlock(pfx, kind) {
  return `
<details class="seo">
  <summary>SEO and social sharing <span class="seo-note">title, description, image, indexing</span></summary>
  <div class="seo-grid">
    <label class="field">Title tag override <input id="${pfx}-seotitle" maxlength="120" placeholder="Defaults to the ${kind} title, followed by the site name"></label>
    <label class="field">Meta description <textarea id="${pfx}-desc" rows="2" maxlength="320" placeholder="Shown in search results and social cards${kind === "post" ? ". Defaults to the summary, then the first paragraph." : ". Defaults to the site meta description."}"></textarea><span class="hint" id="${pfx}-desc-count"></span></label>
    <label class="field">Social image <input id="${pfx}-image" placeholder="assets/cover.jpg or a full URL. Defaults to the site image."></label>
    <label class="field">Canonical URL <input id="${pfx}-canonical" placeholder="Leave blank unless this ${kind} is also published at another URL"></label>
    <label><input type="checkbox" id="${pfx}-noindex"> Ask search engines not to index this ${kind}</label>
    <div class="field"><span class="hint">Search result preview</span><div class="snippet" id="${pfx}-snippet" hidden></div></div>
  </div>
</details>`;
}

function setSeo(pfx, rec) {
  $("#" + pfx + "-seotitle").value = rec.seo_title || "";
  $("#" + pfx + "-desc").value = rec.description || "";
  $("#" + pfx + "-image").value = rec.image || "";
  $("#" + pfx + "-canonical").value = rec.canonical || "";
  $("#" + pfx + "-noindex").checked = !!rec.noindex;
}

function seoVals(pfx) {
  return {
    seo_title: $("#" + pfx + "-seotitle").value,
    description: $("#" + pfx + "-desc").value,
    image: $("#" + pfx + "-image").value,
    canonical: $("#" + pfx + "-canonical").value,
    noindex: $("#" + pfx + "-noindex").checked,
  };
}

function descCount(id) {
  const el = $("#" + id + "-count");
  if (!el) return;
  const n = ($("#" + id).value || "").trim().length;
  el.textContent = n ? n + " characters" + (n > 160 ? ", may be shortened in search results" : "") : "";
  el.className = "hint" + (n > 160 ? " warn" : "");
}

function bindSeo(pfx) {
  ["seotitle", "desc", "image", "canonical"].forEach((k) => {
    const el = $("#" + pfx + "-" + k);
    if (el) el.addEventListener("input", markDirty);
  });
  $("#" + pfx + "-noindex").addEventListener("change", markDirty);
  $("#" + pfx + "-desc").addEventListener("input", () => descCount(pfx + "-desc"));
  descCount(pfx + "-desc");
}

function updateSeoUi(seo) {
  if (!view.pfx || !seo) return;
  const snip = $("#" + view.pfx + "-snippet");
  if (!snip) return;
  snip.hidden = false;
  snip.innerHTML = "";
  const parts = [
    ["snip-url", seo.canonical || seo.rel || ""],
    ["snip-title", seo.full_title || ""],
    ["snip-desc", seo.description || "No meta description set. Search engines will choose text from the page."],
  ];
  parts.forEach(([cls, text]) => {
    const el = document.createElement("div");
    el.className = cls;
    el.textContent = text;
    snip.appendChild(el);
  });
  if (seo.noindex) {
    const w = document.createElement("div");
    w.className = "snip-flag";
    w.textContent = "Marked noindex, so this will be kept out of search engines.";
    snip.appendChild(w);
  }
}

/* ---------- preview ---------- */
function schedulePreview() { clearTimeout(timer); timer = setTimeout(renderPreview, 250); }

function previewPayload() {
  switch (view.type) {
    case "page": return { view: "page", slug: view.slug, title: $("#p-title").value, show_title: $("#p-show").checked, markdown: $("#md").value, ...seoVals("p") };
    case "post": return { view: "post", slug: $("#t-slug").value || slugify($("#t-title").value), title: $("#t-title").value, date: $("#t-date").value, summary: $("#t-summary").value, tags: $("#t-tags").value, markdown: $("#md").value, ...seoVals("t") };
    case "css": return { view: "css", target: $("#css-target").value, css: $("#css").value };
    case "menu": return { view: "menu", target: "page:index", nav: navDraft };
    case "settings": return { view: "settings", target: "page:index", site: readSettings() };
  }
  return null;
}

async function renderPreview() {
  const p = previewPayload();
  if (!p) return;
  try {
    const r = await api("POST", "preview", p);
    const f = $("#preview");
    let y = 0;
    try { y = f.contentWindow.scrollY || 0; } catch (e) { y = 0; }
    f.onload = () => { try { f.contentWindow.scrollTo(0, y); } catch (e) { /* ignore */ } };
    f.srcdoc = r.html;
    updateSeoUi(r.seo);
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- markdown toolbar ---------- */
function insertAt(ta, text) {
  ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, "end");
  ta.dispatchEvent(new Event("input"));
  ta.focus();
}

function applyMd(ta, kind) {
  const s = ta.selectionStart, e = ta.selectionEnd, sel = ta.value.slice(s, e);
  const wrap = (a, b, ph) => {
    const t = sel || ph;
    ta.setRangeText(a + t + b, s, e, "end");
    ta.setSelectionRange(s + a.length, s + a.length + t.length);
  };
  const prefix = (p) => {
    const ls = ta.value.lastIndexOf("\n", s - 1) + 1;
    ta.setRangeText(p, ls, ls, "preserve");
    ta.setSelectionRange(s + p.length, e + p.length);
  };
  if (kind === "bold") wrap("**", "**", "bold text");
  else if (kind === "italic") wrap("*", "*", "italic text");
  else if (kind === "code") wrap("`", "`", "code");
  else if (kind === "link") wrap("[", "](https://)", "link text");
  else if (kind === "heading") prefix("## ");
  else if (kind === "list") prefix("- ");
  else if (kind === "quote") prefix("> ");
  ta.dispatchEvent(new Event("input"));
  ta.focus();
}

async function uploadImage(file, ta) {
  const r = await fetch("/api/upload?name=" + encodeURIComponent(file.name), { method: "POST", headers: { "X-Token": TOKEN }, body: file });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "Upload failed");
  insertAt(ta, "![" + file.name.replace(/\.[^.]+$/, "") + "](" + d.path + ")");
  toast("Image added to assets");
}

function bindEditor() {
  const ta = $("#md");
  ta.addEventListener("input", markDirty);
  $$(".md-tools button").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.md === "image") $("#img-file").click(); else applyMd(ta, b.dataset.md);
  }));
  $("#img-file").addEventListener("change", async (ev) => {
    const f = ev.target.files[0];
    ev.target.value = "";
    if (f) { try { await uploadImage(f, ta); } catch (e) { toast(e.message, "bad"); } }
  });
}

/* ---------- sidebar and top bar ---------- */
function renderTop() {
  const has = (k) => S.pages.some((p) => p.kind === k);
  $("#b-blog").textContent = has("blog") ? "Blog feed" : "Define blog feed";
  $("#b-about").textContent = has("about") ? "About page" : "Define about page";
  $("#b-privacy").textContent = has("privacy") ? "Privacy policy" : "Define privacy policy";
  $("#folder").textContent = S.folder;
  $("#folder").title = S.folder;
}

function renderSide() {
  const side = $("#side");
  side.innerHTML = "";
  const h = (t) => { const e = document.createElement("h3"); e.textContent = t; side.appendChild(e); };
  const item = (label, chip, active, fn) => {
    const b = document.createElement("button");
    b.className = "item" + (active ? " active" : "");
    const t = document.createElement("span"); t.className = "t"; t.textContent = label; b.appendChild(t);
    if (chip) { const c = document.createElement("span"); c.className = "chip " + chip[1]; c.textContent = chip[0]; b.appendChild(c); }
    b.addEventListener("click", fn);
    side.appendChild(b);
  };
  h("Pages");
  S.pages.forEach((p) => item(p.title, null, view.type === "page" && view.slug === p.slug, () => go(() => openPage(p.slug))));
  h("Blog posts");
  const posts = S.posts.slice().sort((a, b) => (b.date + b.created_at).localeCompare(a.date + a.created_at));
  if (!posts.length) { const e = document.createElement("div"); e.className = "empty"; e.textContent = "No posts yet."; side.appendChild(e); }
  posts.forEach((p) => item(p.title, p.status === "published" ? ["Published", "pub"] : ["Draft", "draft"], view.type === "post" && view.slug === p.slug, () => go(() => openPost(p.slug))));
}

async function refresh() { S = await api("GET", "state"); renderTop(); renderSide(); }
async function go(fn) {
  if (!(await guard())) return;
  setDirty(false);
  try { await fn(); } catch (e) { toast(e.message, "bad"); }
}

/* ---------- pages ---------- */
async function openPage(slug) {
  const d = await api("GET", "pages/" + encodeURIComponent(slug));
  view = { type: "page", slug, pfx: "p" };
  $("#left").innerHTML = PAGE_TPL.replace("SEOBLOCK", seoBlock("p", "page")).replace("MDTOOLS", MD_TOOLS);
  $("#p-title").value = d.page.title;
  $("#p-show").checked = d.page.show_title;
  $("#md").value = d.markdown;
  $("#p-file").textContent = d.file;
  $("#p-delete").hidden = d.page.kind === "home";
  $("#p-title").addEventListener("input", markDirty);
  $("#p-show").addEventListener("change", markDirty);
  $("#p-save").addEventListener("click", savePage);
  $("#p-delete").addEventListener("click", deletePage);
  setSeo("p", d.page);
  bindSeo("p");
  bindEditor();
  renderSide();
  renderPreview();
}

async function savePage() {
  try {
    await api("PUT", "pages/" + encodeURIComponent(view.slug), { title: $("#p-title").value, show_title: $("#p-show").checked, markdown: $("#md").value, ...seoVals("p") });
    setDirty(false);
    await refresh();
    toast("Saved");
  } catch (e) { toast(e.message, "bad"); }
}

async function deletePage() {
  if (!window.confirm("Delete this page and its generated file? This cannot be undone.")) return;
  try {
    await api("DELETE", "pages/" + encodeURIComponent(view.slug));
    setDirty(false);
    await refresh();
    toast("Deleted");
    await openPage("index");
  } catch (e) { toast(e.message, "bad"); }
}

async function ensureKind(kind, title) {
  let p = S.pages.find((x) => x.kind === kind);
  if (!p) {
    const r = await api("POST", "pages", { title, kind });
    await refresh();
    p = S.pages.find((x) => x.slug === r.slug);
  }
  return p;
}

/* ---------- posts ---------- */
let slugTouched = false;

async function openPost(slug) {
  let post, markdown;
  if (slug) {
    const d = await api("GET", "posts/" + encodeURIComponent(slug));
    post = d.post; markdown = d.markdown;
  } else {
    post = { slug: "", title: "", date: today(), summary: "", status: "new" };
    markdown = "";
  }
  view = { type: "post", slug: post.slug, status: post.status, pfx: "t" };
  $("#left").innerHTML = POST_TPL.replace("SEOBLOCK", seoBlock("t", "post")).replace("MDTOOLS", MD_TOOLS);
  $("#t-title").value = post.title;
  $("#t-date").value = post.date;
  $("#t-slug").value = post.slug;
  $("#t-summary").value = post.summary || "";
  $("#t-tags").value = (post.tags || []).join(", ");
  $("#md").value = markdown;
  slugTouched = !!slug;
  $("#t-title").addEventListener("input", () => {
    if (!slugTouched && view.status !== "published") $("#t-slug").value = slugify($("#t-title").value);
    markDirty();
  });
  $("#t-slug").addEventListener("input", () => { slugTouched = true; markDirty(); });
  $("#t-date").addEventListener("input", markDirty);
  $("#t-summary").addEventListener("input", markDirty);
  $("#t-tags").addEventListener("input", () => { renderTagPick(); markDirty(); });
  $("#t-save").addEventListener("click", () => savePost(false));
  $("#t-publish").addEventListener("click", () => savePost(true));
  $("#t-unpublish").addEventListener("click", unpublishPost);
  $("#t-delete").addEventListener("click", deletePost);
  setSeo("t", post);
  bindSeo("t");
  bindEditor();
  syncPostButtons();
  renderTagPick();
  renderSide();
  renderPreview();
  if (!slug) $("#t-title").focus();
}

function parseTags(v) { return v.split(",").map((x) => x.trim()).filter(Boolean); }

function renderTagPick() {
  const box = $("#t-tagpick");
  if (!box) return;
  box.innerHTML = "";
  const current = parseTags($("#t-tags").value).map(slugify);
  const known = new Map();
  S.posts.forEach((p) => (p.tags || []).forEach((t) => { if (slugify(t) && !known.has(slugify(t))) known.set(slugify(t), t); }));
  if (!known.size) {
    const h = document.createElement("span");
    h.className = "hint";
    h.textContent = "Tags you use will appear here for one click reuse.";
    box.appendChild(h);
    return;
  }
  known.forEach((name, key) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = name;
    const on = current.includes(key);
    b.className = on ? "on" : "";
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.addEventListener("click", () => {
      let list = parseTags($("#t-tags").value);
      if (list.some((x) => slugify(x) === key)) list = list.filter((x) => slugify(x) !== key); else list.push(name);
      $("#t-tags").value = list.join(", ");
      renderTagPick();
      markDirty();
    });
    box.appendChild(b);
  });
}

function syncPostButtons() {
  const pub = view.status === "published";
  const chip = $("#t-status");
  chip.textContent = pub ? "Published" : view.status === "draft" ? "Draft" : "New";
  chip.className = "chip " + (pub ? "pub" : "draft");
  $("#t-save").hidden = pub;
  $("#t-unpublish").hidden = !pub;
  $("#t-publish").textContent = pub ? "Update post" : "Publish";
  $("#t-slug").disabled = pub;
  $("#t-delete").hidden = !view.slug;
}

async function savePost(publish) {
  const body = {
    slug: view.slug || "", new_slug: $("#t-slug").value, title: $("#t-title").value, date: $("#t-date").value,
    summary: $("#t-summary").value, tags: $("#t-tags").value, markdown: $("#md").value, publish, ...seoVals("t"),
  };
  try {
    const r = await api("POST", "posts", body);
    view.slug = r.slug; view.status = r.post.status;
    $("#t-slug").value = r.slug;
    setDirty(false);
    await refresh();
    syncPostButtons();
    renderTagPick();
    let msg = r.was_published ? "Updated" : publish ? "Published" : "Saved";
    if (r.feed_created) msg += ". Blog feed page created.";
    toast(msg);
  } catch (e) { toast(e.message, "bad"); }
}

async function unpublishPost() {
  if (!window.confirm("Unpublish this post? It goes back to drafts and leaves the blog feed.")) return;
  try {
    await api("POST", "posts/" + encodeURIComponent(view.slug) + "/unpublish");
    view.status = "draft";
    await refresh();
    syncPostButtons();
    toast("Unpublished");
  } catch (e) { toast(e.message, "bad"); }
}

async function deletePost() {
  if (!window.confirm("Delete this post permanently? This cannot be undone.")) return;
  try {
    await api("DELETE", "posts/" + encodeURIComponent(view.slug));
    setDirty(false);
    await refresh();
    toast("Deleted");
    await openPage("index");
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- css ---------- */
async function openCss() {
  const d = await api("GET", "css");
  view = { type: "css" };
  $("#left").innerHTML = CSS_TPL;
  const th = $("#theme");
  S.themes.forEach((t) => { const o = document.createElement("option"); o.value = t.id; o.textContent = t.label; th.appendChild(o); });
  const tg = $("#css-target");
  S.pages.forEach((p) => { const o = document.createElement("option"); o.value = "page:" + p.slug; o.textContent = "Page: " + p.title; tg.appendChild(o); });
  S.posts.forEach((p) => { const o = document.createElement("option"); o.value = "post:" + p.slug; o.textContent = "Post: " + p.title; tg.appendChild(o); });
  $("#css").value = d.css;
  $("#css").addEventListener("input", markDirty);
  tg.addEventListener("change", renderPreview);
  $("#css-save").addEventListener("click", saveCss);
  $("#apply-theme").addEventListener("click", applyTheme);
  $("#jump-global").addEventListener("click", () => jumpTo("GLOBAL THEME"));
  $("#jump-pages").addEventListener("click", () => jumpTo("PAGE SPECIFIC RULES"));
  renderSide();
  renderPreview();
}

function applyTheme() {
  const t = S.themes.find((x) => x.id === $("#theme").value);
  const ta = $("#css");
  if (!window.confirm("Replace the theme variables at the top of your CSS with \"" + t.label + "\"?\n\nAny edits you made inside that top :root block are overwritten. Your base styles and page specific rules stay as they are.")) return;
  const re = /:root\s*\{[^}]*\}/;
  ta.value = re.test(ta.value) ? ta.value.replace(re, () => t.block) : t.block + "\n\n" + ta.value;
  markDirty();
}

function jumpTo(marker) {
  const ta = $("#css");
  const i = ta.value.indexOf(marker);
  if (i < 0) { toast("That section marker is not in your CSS any more.", "bad"); return; }
  const lh = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  const line = ta.value.slice(0, i).split("\n").length - 1;
  ta.focus();
  ta.setSelectionRange(i, i);
  ta.scrollTop = Math.max(0, (line - 1) * lh);
}

async function saveCss() {
  try {
    await api("PUT", "css", { css: $("#css").value });
    setDirty(false);
    toast("Saved");
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- menu ---------- */
async function openMenu() {
  navDraft = JSON.parse(JSON.stringify(S.nav));
  view = { type: "menu" };
  $("#left").innerHTML = MENU_TPL;
  $("#nav-save").addEventListener("click", saveMenu);
  $("#nl-add").addEventListener("click", () => {
    const label = $("#nl-label").value.trim(), url = $("#nl-url").value.trim();
    if (!label || !url) { toast("Enter both a label and an address.", "bad"); return; }
    navDraft.push({ type: "link", label, url, visible: true });
    $("#nl-label").value = ""; $("#nl-url").value = "";
    renderNavRows(); markDirty();
  });
  renderNavRows();
  renderSide();
  renderPreview();
}

function renderNavRows() {
  const box = $("#nav-rows");
  box.innerHTML = "";
  navDraft.forEach((n, i) => {
    const row = document.createElement("div");
    row.className = "nav-row" + (n.visible ? "" : " off");
    row.innerHTML = `<label><input type="checkbox" class="n-vis"> In menu</label><input class="n-label" aria-label="Menu label"><span class="n-target"></span><input class="n-url" aria-label="Link address" placeholder="https://example.com"><span class="grow"></span><button class="n-up">Up</button><button class="n-down">Down</button><button class="n-del danger">Remove</button>`;
    const page = n.type === "page" ? S.pages.find((p) => p.slug === n.slug) : null;
    $(".n-vis", row).checked = n.visible;
    $(".n-label", row).value = n.label;
    $(".n-target", row).textContent = page ? (page.kind === "home" ? "index.html" : page.kind === "blog" ? "blog/index.html" : page.slug + ".html") : "";
    $(".n-target", row).hidden = !page;
    $(".n-url", row).value = n.url || "";
    $(".n-url", row).hidden = !!page;
    $(".n-del", row).hidden = !!page;
    $(".n-vis", row).addEventListener("change", (ev) => { n.visible = ev.target.checked; row.classList.toggle("off", !n.visible); markDirty(); });
    $(".n-label", row).addEventListener("input", (ev) => { n.label = ev.target.value; markDirty(); });
    $(".n-url", row).addEventListener("input", (ev) => { n.url = ev.target.value; markDirty(); });
    $(".n-up", row).addEventListener("click", () => { if (i > 0) { [navDraft[i - 1], navDraft[i]] = [navDraft[i], navDraft[i - 1]]; renderNavRows(); markDirty(); } });
    $(".n-down", row).addEventListener("click", () => { if (i < navDraft.length - 1) { [navDraft[i + 1], navDraft[i]] = [navDraft[i], navDraft[i + 1]]; renderNavRows(); markDirty(); } });
    $(".n-del", row).addEventListener("click", () => { navDraft.splice(i, 1); renderNavRows(); markDirty(); });
    box.appendChild(row);
  });
}

async function saveMenu() {
  try {
    await api("PUT", "nav", { nav: navDraft });
    setDirty(false);
    await refresh();
    toast("Saved");
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- settings ---------- */
function readSettings() {
  return {
    title: $("#s-title").value, tagline: $("#s-tagline").value, description: $("#s-description").value,
    footer: $("#s-footer").value, author: $("#s-author").value, lang: $("#s-lang").value,
    url: $("#s-url").value, rss: $("#s-rss").checked, sitemap: $("#s-sitemap").checked,
    image: $("#s-image").value, twitter: $("#s-twitter").value, noindex: $("#s-noindex").checked,
  };
}

async function openSettings() {
  view = { type: "settings" };
  $("#left").innerHTML = SET_TPL;
  const s = S.site;
  $("#s-title").value = s.title; $("#s-tagline").value = s.tagline; $("#s-footer").value = s.footer;
  $("#s-author").value = s.author; $("#s-lang").value = s.lang;
  $("#s-description").value = s.description || "";
  $("#s-url").value = s.url || ""; $("#s-rss").checked = !!s.rss; $("#s-sitemap").checked = !!s.sitemap;
  $("#s-image").value = s.image || ""; $("#s-twitter").value = s.twitter || ""; $("#s-noindex").checked = !!s.noindex;
  const addr = () => {
    const base = ($("#s-url").value.trim().replace(/\/+$/, "")) || "your-site-url";
    $("#s-rss-addr").textContent = base + "/rss";
    $("#s-sitemap-addr").textContent = base + "/sitemap.xml";
  };
  addr();
  descCount("s-description");
  $$("#left input, #left textarea").forEach((i) => {
    i.addEventListener(i.type === "checkbox" ? "change" : "input", () => { addr(); descCount("s-description"); markDirty(); });
  });
  $("#s-save").addEventListener("click", saveSettings);
  renderSide();
  renderPreview();
}

async function saveSettings() {
  try {
    await api("PUT", "settings", readSettings());
    setDirty(false);
    await refresh();
    toast("Saved");
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- top bar actions ---------- */
function themeLabel() {
  $("#b-theme").textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "Light mode" : "Dark mode";
}

function toggleTheme() {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("sitegen-theme", next); } catch (e) { /* ignore */ }
  themeLabel();
}

async function act(name) {
  if (name === "theme") { toggleTheme(); return; }
  if (name === "quit") {
    if (!(await guard())) return;
    await api("POST", "quit");
    document.body.innerHTML = "<p style='padding:40px;font:16px system-ui'>SiteGen has stopped. You can close this tab.</p>";
    return;
  }
  await go(async () => {
    if (name === "add-page") {
      const title = window.prompt("Page title");
      if (!title || !title.trim()) return;
      const r = await api("POST", "pages", { title: title.trim(), kind: "page" });
      await refresh();
      await openPage(r.slug);
    } else if (name === "blog") { await openPage((await ensureKind("blog", "Blog")).slug); }
    else if (name === "about") { await openPage((await ensureKind("about", "About")).slug); }
    else if (name === "privacy") { await openPage((await ensureKind("privacy", "Privacy policy")).slug); }
    else if (name === "add-post") { await openPost(null); }
    else if (name === "css") { await openCss(); }
    else if (name === "menu") { await openMenu(); }
    else if (name === "settings") { await openSettings(); }
    else if (name === "switch") { await chooseFolder(); }
  });
}

/* ---------- folders ---------- */
async function openFolder(path, ok) {
  const r = await api("POST", "open", { path, confirm: !!ok });
  if (r.needs_confirm) {
    if (window.confirm(r.message)) return openFolder(path, true);
    return;
  }
  S = r.state;
  await boot();
}

async function chooseFolder(inputEl) {
  const r = await api("POST", "pick-folder");
  let path = r.path;
  if (!r.available) path = window.prompt("Folder path for your site", (S && S.folder) || (inputEl && inputEl.value) || "");
  if (!path) return;
  if (inputEl) inputEl.value = path;
  await openFolder(path, false);
}

function showWelcome() {
  $("#workspace").hidden = true;
  $("#welcome").hidden = false;
  $("#w-path").value = S.last || S.suggest || "";
  $("#w-browse").onclick = () => chooseFolder($("#w-path")).catch((e) => toast(e.message, "bad"));
  $("#w-open").onclick = () => openFolder($("#w-path").value, false).catch((e) => toast(e.message, "bad"));
}

async function boot() {
  $("#welcome").hidden = true;
  $("#workspace").hidden = false;
  setDirty(false);
  view = { type: "none" };
  renderTop();
  renderSide();
  await openPage("index");
}

$$("#top [data-act]").forEach((b) => b.addEventListener("click", () => act(b.dataset.act)));

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (view.type === "page") savePage();
    else if (view.type === "post") savePost(view.status === "published");
    else if (view.type === "css") saveCss();
    else if (view.type === "menu") saveMenu();
    else if (view.type === "settings") saveSettings();
  }
});
window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

themeLabel();

(async function init() {
  try {
    S = await api("GET", "state");
    if (!S.folder) showWelcome(); else await boot();
  } catch (e) { toast(e.message, "bad"); }
})();
</script>
</body>
</html>
'''


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="SiteGen: local static site generator with a markdown editor and live preview.")
    ap.add_argument("folder", nargs="?", help="site folder to open (created if it does not exist)")
    ap.add_argument("--port", type=int, default=8765, help="port for the local editor (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab automatically")
    args = ap.parse_args()

    if args.folder:
        try:
            r = open_site(args.folder)
            if r.get("needs_confirm"):
                print("That folder has files SiteGen did not create. Open it from the editor to confirm.")
        except ApiError as e:
            print("Could not open folder:", e)
    else:
        last = last_folder()
        if last and (Path(last) / META_DIR / "config.json").exists():
            try:
                open_site(last)
            except Exception as e:  # noqa: BLE001
                print("Could not reopen the last site:", e)

    httpd = None
    for port in (args.port, 0):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        sys.exit("Could not start the local server.")
    httpd.daemon_threads = True
    SERVER["httpd"] = httpd
    SERVER["port"] = httpd.server_address[1]
    url = "http://127.0.0.1:%d/" % SERVER["port"]
    print("SiteGen is running at %s (Ctrl+C to stop)" % url)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
