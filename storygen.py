#!/usr/bin/env python3
"""
StoryGen: a local fiction site generator with a markdown editor and live preview.

Run it:
    python3 storygen.py                opens the last library you used
    python3 storygen.py ~/my-stories   opens (or creates) a library in that folder
    python3 storygen.py --help

It needs Python 3.8 or newer and nothing else (standard library only).
It starts a small web server on 127.0.0.1 and opens the editor in your browser.
The editor uses JavaScript. The sites it generates use none: only HTML and CSS.

It is built for short stories, novellas and serials: one library page, one page per story,
and one page per chapter with the chapter list down the left side.

Folder layout (inside the library folder you choose):
    index.html                      generated library page
    about.html, ...                 generated extra pages
    <story>/index.html              generated story page (cover, blurb, chapter list)
    <story>/<chapter>.html          generated chapters
    genre/<genre>.html              generated genre listings
    style.css                       your stylesheet (edited through "Edit CSS", never overwritten)
    assets/                         covers and other images you add from the editor
    rss, rss.xml                    optional feed of new chapters
    .storygen/                      your markdown sources and settings:
        config.json                 stories, chapter order, settings
        home.md                     the library page introduction
        pages/<slug>.md             extra pages
        stories/<story>/_index.md   a standalone story's text, or a foreword
        stories/<story>/<chapter>.md

You can also write in any editor: drop .md files into a folder under .storygen/stories/
and press "Rescan files". New folders become stories.

Do not hand-edit the generated .html files, they are rewritten on every save.
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
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

APP = "StoryGen"
META_DIR = ".storygen"
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.RLock()
CURRENT = {"site": None}
SERVER = {"httpd": None, "port": 0}
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "storygen"
RESERVED_SLUGS = {"index", "style", "assets", "genre", "rss", "sitemap", "robots", "site", "api", "404", "search"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}
MAX_UPLOAD = 25 * 1024 * 1024
STATUSES = [
    ("draft", "Draft"),
    ("ongoing", "Ongoing"),
    ("hiatus", "On hiatus"),
    ("complete", "Complete"),
]
STATUS_LABELS = dict(STATUSES)


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
# Raw HTML inside markdown is passed through an allowlist sanitizer built on html.parser.
# The parser decodes entities in attribute values the same way a browser does, so tricks such as
# href="&#106;avascript:..." are caught. Tags outside the allowlist are dropped (script and style
# lose their contents too), event handler attributes are removed and URLs are checked by scheme.
_SAFE_TAGS = BLOCK_TAGS | INLINE_TAGS | set(
    "pre caption colgroup col tfoot track main small".split()
)
_DROP_CONTENT = {"script", "style", "template", "noscript"}
_URL_ATTRS = {"href", "src", "poster", "action", "formaction", "cite", "data", "background", "longdesc"}
_SAFE_SCHEMES = ("http:", "https:", "mailto:", "tel:")
_ATTR_NAME = re.compile(r"^[a-zA-Z_:][-a-zA-Z0-9_:.]*$")
_BAD_CSS = re.compile(r"expression|javascript|vbscript|behavior|-moz-binding|@import", re.I)


def safe_url(value, prefix, attr_name="href"):
    """Return a URL that is safe to emit, or '#'. Relative URLs get the page prefix."""
    v = str(value or "").strip()
    compact = re.sub(r"[\s\x00-\x1f\x7f]+", "", v).lower()
    if not compact:
        return "#" if attr_name == "href" else ""
    if _SCHEME.match(compact):
        if compact.startswith(_SAFE_SCHEMES):
            return v
        if attr_name in ("src", "poster") and re.match(r"^data:image/(png|jpe?g|gif|webp|avif);", compact):
            return v
        return "#" if attr_name in ("href", "action") else ""
    return fix_url(v, prefix)


def _safe_srcset(value, prefix):
    out = []
    for cand in str(value).split(","):
        bits = cand.strip().split()
        if not bits:
            continue
        url = safe_url(bits[0], prefix, "src")
        if not url or url == "#":
            return ""
        out.append(" ".join([url] + bits[1:]))
    return ", ".join(out)


class _Sanitizer(HTMLParser):
    def __init__(self, prefix):
        super().__init__(convert_charrefs=True)
        self.prefix = prefix
        self.out = []
        self.skip = 0

    def _attrs(self, attrs):
        parts = []
        for name, value in attrs:
            name = (name or "").lower()
            if not _ATTR_NAME.match(name) or name.startswith("on") or name in ("srcdoc", "formaction"):
                continue
            if value is None:
                parts.append(" " + name)
                continue
            if name in _URL_ATTRS or name.endswith(":href"):
                value = safe_url(value, self.prefix, name)
                if not value:
                    continue
            elif name == "srcset":
                value = _safe_srcset(value, self.prefix)
                if not value:
                    continue
            elif name == "style":
                if _BAD_CSS.search(re.sub(r"/\*.*?\*/|\\", "", value, flags=re.S)):
                    continue
            parts.append(' %s="%s"' % (name, attr(value)))
        return "".join(parts)

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in _SAFE_TAGS:
            return
        self.out.append("<%s%s>" % (tag, self._attrs(attrs)))

    def handle_startendtag(self, tag, attrs):
        if self.skip or tag not in _SAFE_TAGS:
            return
        self.out.append("<%s%s>" % (tag, self._attrs(attrs)))

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in _SAFE_TAGS:
            return
        self.out.append("</%s>" % tag)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(esc(data))

    # comments, doctype, processing instructions and CDATA are dropped
    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass


def sanitize_html(s, prefix=""):
    p = _Sanitizer(prefix)
    try:
        p.feed(s)
        p.close()
    except Exception:  # malformed input: fall back to showing it as text
        return esc(s)
    return "".join(p.out)


class Markdown:
    def __init__(self, prefix=""):
        self.prefix = prefix
        self.ids = {}

    # ---- entry points
    def render(self, text):
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").expandtabs(4)
        return self.blocks(text.split("\n"))

    def raw(self, s):
        return sanitize_html(s, self.prefix)

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
FONT_BOOK = '"Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif'

THEME_DEFAULTS = {
    "bg": "#f3f1ec",
    "surface": "#fffdf8",
    "text": "#23201b",
    "muted": "#6b655c",
    "accent": "#8a5a2b",
    "border": "#ddd6c8",
    "header-bg": "#fffdf8",
    "header-text": "#23201b",
    "sidebar-bg": "#f3f1ec",
    "sidebar-text": "#3a352d",
    "sidebar-active": "#e6e0d2",
    "code-bg": "#efeadf",
    "font-body": FONT_BOOK,
    "font-heading": FONT_SERIF,
    "font-ui": FONT_SANS,
    "font-mono": FONT_MONO,
    "base-size": "18px",
    "line-height": "1.75",
    "measure": "34rem",
    "sidebar-width": "16rem",
    "radius": "4px",
    "chapter-number": "0.78rem",
    "dropcap-size": "3.1em",
}
VAR_ORDER = list(THEME_DEFAULTS.keys())

THEMES = {
    "paperback": ("Paperback", {}),
    "night": ("Night reading", {
        "bg": "#14161a", "surface": "#1a1d22", "text": "#ddd8cf", "muted": "#9a948a", "accent": "#d3a06a",
        "border": "#2c3139", "header-bg": "#14161a", "header-text": "#ddd8cf", "sidebar-bg": "#171a1f",
        "sidebar-text": "#c9c3ba", "sidebar-active": "#232830", "code-bg": "#101317",
    }),
    "parchment": ("Parchment", {
        "bg": "#e7dec7", "surface": "#f6efdc", "text": "#2f2a20", "muted": "#6d6552", "accent": "#7a5230",
        "border": "#d0c4a4", "header-bg": "#f6efdc", "header-text": "#2f2a20", "sidebar-bg": "#ebe2cc",
        "sidebar-text": "#3a3427", "sidebar-active": "#ded3b6", "code-bg": "#e9e0c6",
        "base-size": "19px", "line-height": "1.8",
    }),
    "manuscript": ("Manuscript", {
        "bg": "#ffffff", "surface": "#ffffff", "text": "#111111", "muted": "#555555", "accent": "#222222",
        "border": "#cccccc", "header-bg": "#ffffff", "header-text": "#111111", "sidebar-bg": "#f4f4f4",
        "sidebar-text": "#222222", "sidebar-active": "#e6e6e6", "code-bg": "#f4f4f4",
        "font-body": FONT_MONO, "font-heading": FONT_MONO, "base-size": "16px", "line-height": "2",
        "measure": "36rem", "radius": "0px", "dropcap-size": "1em",
    }),
    "ink": ("Ink on white", {
        "bg": "#ffffff", "surface": "#ffffff", "text": "#16181d", "muted": "#5f646e", "accent": "#1b3f8b",
        "border": "#e2e4e8", "header-bg": "#ffffff", "header-text": "#16181d", "sidebar-bg": "#f7f8fa",
        "sidebar-text": "#31353d", "sidebar-active": "#eaecf1", "code-bg": "#f4f5f7",
        "font-body": FONT_SERIF, "radius": "2px",
    }),
    "midnight": ("Midnight blue", {
        "bg": "#0f1420", "surface": "#161d2b", "text": "#e3e8f2", "muted": "#98a2b6", "accent": "#8fb3ff",
        "border": "#27324a", "header-bg": "#0f1420", "header-text": "#e3e8f2", "sidebar-bg": "#131a28",
        "sidebar-text": "#ccd4e3", "sidebar-active": "#1f2839", "code-bg": "#0b101a",
        "font-body": FONT_SERIF,
    }),
    "sepia": ("Sepia", {
        "bg": "#efe3d0", "surface": "#f9f0e1", "text": "#3b2f22", "muted": "#7b6a55", "accent": "#9a4f24",
        "border": "#dac9ae", "header-bg": "#f9f0e1", "header-text": "#3b2f22", "sidebar-bg": "#ecdfca",
        "sidebar-text": "#453726", "sidebar-active": "#e0cfb2", "code-bg": "#eadcc4",
    }),
    "noir": ("Noir", {
        "bg": "#0c0c0d", "surface": "#141415", "text": "#e8e6e3", "muted": "#8e8b86", "accent": "#c3453b",
        "border": "#27272a", "header-bg": "#0c0c0d", "header-text": "#e8e6e3", "sidebar-bg": "#0f0f10",
        "sidebar-text": "#cfcdc9", "sidebar-active": "#1d1d20", "code-bg": "#0a0a0b",
        "font-heading": FONT_SANS, "radius": "0px",
    }),
    "pulp": ("Pulp", {
        "bg": "#1b1712", "surface": "#241e17", "text": "#f2e6d2", "muted": "#b3a48c", "accent": "#e0a63c",
        "border": "#3a3126", "header-bg": "#1b1712", "header-text": "#f2e6d2", "sidebar-bg": "#1f1a14",
        "sidebar-text": "#e2d6c0", "sidebar-active": "#312819", "code-bg": "#171310",
        "font-heading": FONT_SANS, "dropcap-size": "3.4em",
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
  StoryGen stylesheet

  TOP:     global theme variables (colors, fonts, sizes). They apply to every page.
  MIDDLE:  base styles: header, chapter sidebar, library cards, prose.
  BOTTOM:  page specific rules. Every page has body classes, so a rule under one
           only affects those pages.

  Reading width is "measure". Paragraph style (indented like a printed book, or
  spaced like a web page) is chosen in Settings and sets body.prose-indented or
  body.prose-spaced.

  StoryGen never overwrites this file. "Apply theme" only replaces the :root block.
*/

/* ============ GLOBAL THEME (every page) ============ */
"""

BASE_CSS = """
/* ============ GLOBAL BASE STYLES (every page) ============ */
*, *::before, *::after { box-sizing: border-box; }
html { font-size: var(--base-size); -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--font-body); line-height: var(--line-height); }
a { color: var(--accent); text-underline-offset: 0.18em; }
a:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 1rem; top: 1rem; z-index: 20; padding: 0.5rem 0.75rem; background: var(--surface); color: var(--text); }
.ui { font-family: var(--font-ui); }

/* header */
.nav-toggle { position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }
.site-header { background: var(--header-bg); color: var(--header-text); border-bottom: 1px solid var(--border); }
.site-header .wrap { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.3rem 1.5rem; padding: 0.9rem 1.25rem; max-width: 72rem; margin: 0 auto; }
.site-title { font-family: var(--font-heading); font-size: 1.35rem; font-weight: 700; color: var(--header-text); text-decoration: none; }
.tagline { color: var(--muted); font-size: 0.85rem; font-family: var(--font-ui); }
.site-nav { margin-left: auto; font-family: var(--font-ui); font-size: 0.9rem; }
.site-nav ul { display: flex; flex-wrap: wrap; gap: 0.25rem 1.1rem; list-style: none; margin: 0; padding: 0; }
.site-nav a { color: var(--header-text); text-decoration: none; border-bottom: 2px solid transparent; }
.site-nav a:hover { border-bottom-color: var(--border); }
.site-nav a[aria-current="page"] { color: var(--accent); border-bottom-color: var(--accent); }
.nav-button { display: none; font-family: var(--font-ui); font-size: 0.85rem; cursor: pointer; user-select: none; padding: 0.15rem 0.7rem; border: 1px solid var(--border); border-radius: var(--radius); }

/* layout */
.plain { max-width: 62rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
.reading { display: grid; grid-template-columns: var(--sidebar-width) minmax(0, 1fr); align-items: start; min-height: calc(100vh - 3.6rem); }
.chapters-nav { min-height: calc(100vh - 3.6rem); }
.chapters-nav {
  position: sticky; top: 0; max-height: 100vh; overflow-y: auto; padding: 1.25rem 0.85rem 2rem;
  background: var(--sidebar-bg); color: var(--sidebar-text); border-right: 1px solid var(--border);
  font-family: var(--font-ui); font-size: 0.88rem;
}
.chapters-nav .in-story { display: block; font-family: var(--font-heading); font-size: 1.05rem; font-weight: 700; color: var(--sidebar-text); text-decoration: none; margin-bottom: 0.15rem; }
.chapters-nav .by { display: block; color: var(--muted); font-size: 0.8rem; margin-bottom: 0.9rem; }
.chapters-nav ol { list-style: none; margin: 0; padding: 0; counter-reset: ch; }
.chapters-nav li { counter-increment: ch; }
.chapters-nav li a { display: flex; gap: 0.5rem; padding: 0.3rem 0.55rem; border-radius: var(--radius); color: var(--sidebar-text); text-decoration: none; }
.chapters-nav li a::before { content: counter(ch); color: var(--muted); font-variant-numeric: tabular-nums; min-width: 1.3em; text-align: right; }
.chapters-nav li a:hover { background: var(--sidebar-active); }
.chapters-nav li a[aria-current="page"] { background: var(--sidebar-active); color: var(--accent); font-weight: 600; }
.chapters-nav .all-stories { display: block; margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid var(--border); color: var(--muted); font-size: 0.82rem; }
.reading main { min-width: 0; padding: 2.5rem clamp(1rem, 5vw, 3rem) 4rem; }
.sheet { max-width: var(--measure); margin: 0 auto; }

/* prose */
.prose { font-size: 1rem; }
.prose p { margin: 0 0 1.15rem; }
body.prose-indented .prose p + p { margin-top: -1.15rem; text-indent: 1.6em; }
body.prose-indented .prose p { margin-bottom: 1.15rem; }
body.prose-indented .prose blockquote p + p, body.prose-indented .prose li p + p { margin-top: 0; }
.prose em { font-style: italic; }
.prose h2, .prose h3 { font-family: var(--font-heading); line-height: 1.25; margin: 2.2rem 0 0.9rem; }
.prose h2 { font-size: 1.35rem; }
.prose h3 { font-size: 1.1rem; }
.prose blockquote { margin: 1.5rem 1.5rem; padding: 0; color: var(--muted); font-style: italic; }
.prose blockquote p + p { text-indent: 0; }
.prose hr { border: 0; margin: 2rem 0; text-align: center; }
.prose hr::after { content: "* * *"; letter-spacing: 0.6em; color: var(--muted); font-size: 0.9rem; }
.prose img { max-width: 100%; height: auto; border-radius: var(--radius); }
.prose code { font-family: var(--font-mono); font-size: 0.88em; background: var(--code-bg); padding: 0.1em 0.35em; border-radius: 3px; }
.prose pre { overflow-x: auto; padding: 0.9rem 1rem; background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--radius); font-size: 0.85rem; line-height: 1.5; }
.prose pre code { background: none; padding: 0; }
.prose ul, .prose ol { margin: 0 0 1.15rem; padding-left: 1.4rem; }
.table-wrap { overflow-x: auto; }
.prose table { border-collapse: collapse; min-width: 100%; font-size: 0.92rem; }
.prose th, .prose td { border: 1px solid var(--border); padding: 0.35rem 0.6rem; text-align: left; }
.task-item { list-style: none; margin-left: -1.25rem; }
body.dropcap .prose.opening > p:first-of-type::first-letter {
  float: left; font-family: var(--font-heading); font-size: var(--dropcap-size); line-height: 0.82;
  padding: 0.08em 0.08em 0 0; color: var(--accent);
}
body.dropcap .prose.opening > p:first-of-type { text-indent: 0; }

/* chapter page */
.chapter-head { margin-bottom: 2.5rem; text-align: center; }
.chapter-head .in { display: block; font-family: var(--font-ui); font-size: var(--chapter-number); letter-spacing: 0.16em; text-transform: uppercase; color: var(--muted); }
.chapter-head .in a { color: inherit; text-decoration: none; }
.chapter-head h1 { font-family: var(--font-heading); font-size: 1.8rem; margin: 0.6rem 0 0.4rem; line-height: 1.2; }
.chapter-head .meta { font-family: var(--font-ui); font-size: 0.8rem; color: var(--muted); margin: 0; }
.note { margin: 2.5rem 0 0; padding: 1rem 1.15rem; background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--radius); font-size: 0.92rem; }
.note h2 { font-family: var(--font-ui); font-size: 0.8rem; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); margin: 0 0 0.5rem; }
.note p { margin: 0 0 0.6rem; text-indent: 0 !important; }
.note > :last-child { margin-bottom: 0; }
.the-end { margin: 3rem 0 0; text-align: center; font-family: var(--font-ui); font-size: 0.78rem; letter-spacing: 0.25em; text-transform: uppercase; color: var(--muted); }
.pager { display: flex; gap: 1rem; margin: 3rem 0 0; font-family: var(--font-ui); font-size: 0.9rem; }
.pager a { flex: 1 1 0; padding: 0.7rem 0.9rem; border: 1px solid var(--border); border-radius: var(--radius); text-decoration: none; color: var(--text); }
.pager a:hover { border-color: var(--accent); }
.pager .next { text-align: right; margin-left: auto; }
.pager small { display: block; color: var(--muted); font-size: 0.72rem; letter-spacing: 0.1em; text-transform: uppercase; }

/* story page and library */
.story-head { display: flex; flex-wrap: wrap; gap: 1.5rem; margin-bottom: 2rem; }
.story-head .cover { flex: 0 0 auto; width: 11rem; max-width: 40%; border: 1px solid var(--border); border-radius: var(--radius); }
.story-head .about { flex: 1 1 18rem; min-width: 0; }
.story-head h1 { font-family: var(--font-heading); font-size: 2rem; margin: 0 0 0.2rem; line-height: 1.15; }
.story-head .subtitle { font-family: var(--font-heading); font-style: italic; color: var(--muted); margin: 0 0 0.5rem; }
.byline { font-family: var(--font-ui); font-size: 0.9rem; color: var(--muted); margin: 0 0 0.75rem; }
.facts { display: flex; flex-wrap: wrap; gap: 0.4rem 0.75rem; list-style: none; margin: 0 0 1rem; padding: 0; font-family: var(--font-ui); font-size: 0.8rem; color: var(--muted); }
.tag { display: inline-block; padding: 0.05rem 0.6rem; border: 1px solid var(--border); border-radius: 99px; text-decoration: none; color: var(--muted); }
.tag:hover { color: var(--accent); border-color: var(--accent); }
.status { border-color: var(--accent); color: var(--accent); }
.blurb { margin: 0 0 1rem; }
.warnings { font-family: var(--font-ui); font-size: 0.85rem; color: var(--muted); margin: 0 0 1rem; }
.read-button { display: inline-block; padding: 0.55rem 1.4rem; background: var(--accent); color: var(--surface); border-radius: var(--radius); font-family: var(--font-ui); font-size: 0.92rem; text-decoration: none; }
.read-button:hover { opacity: 0.9; }
.toc { margin: 2.5rem 0 0; }
.toc h2 { font-family: var(--font-ui); font-size: 0.8rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin: 0 0 0.75rem; }
.toc ol { list-style: none; margin: 0; padding: 0; counter-reset: ch; }
.toc li { counter-increment: ch; border-top: 1px solid var(--border); }
.toc li:last-child { border-bottom: 1px solid var(--border); }
.toc a { display: flex; align-items: baseline; gap: 0.75rem; padding: 0.65rem 0.25rem; text-decoration: none; color: var(--text); }
.toc a:hover { background: var(--sidebar-active); }
.toc a::before { content: counter(ch); color: var(--muted); font-variant-numeric: tabular-nums; min-width: 1.6em; text-align: right; font-family: var(--font-ui); font-size: 0.85rem; }
.toc .ch-title { flex: 1; }
.toc .ch-meta { font-family: var(--font-ui); font-size: 0.78rem; color: var(--muted); white-space: nowrap; }

.shelf { display: grid; grid-template-columns: repeat(auto-fill, minmax(17rem, 1fr)); gap: 1.75rem; margin: 2rem 0 0; }
.book { display: flex; gap: 1rem; }
.book > a { flex: 0 0 auto; text-decoration: none; }
.book .cover { flex: 0 0 auto; width: 7.5rem; border: 1px solid var(--border); border-radius: var(--radius); }
.book .cover.none { display: flex; align-items: center; justify-content: center; aspect-ratio: 2 / 3; background: var(--code-bg); color: var(--muted); font-family: var(--font-ui); font-size: 0.72rem; text-align: center; padding: 0.5rem; }
.book h2 { font-family: var(--font-heading); font-size: 1.15rem; margin: 0 0 0.15rem; line-height: 1.25; }
.book h2 a { color: var(--text); text-decoration: none; }
.book h2 a:hover { color: var(--accent); }
.book p { margin: 0 0 0.5rem; font-size: 0.92rem; }
.library-intro { max-width: var(--measure); }
.section-title { font-family: var(--font-ui); font-size: 0.8rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin: 2.5rem 0 0; }
.page-body { max-width: var(--measure); }
.page-body h1 { font-family: var(--font-heading); font-size: 1.9rem; margin: 0 0 1.25rem; }
.updates { list-style: none; margin: 1rem 0 0; padding: 0; }
.updates li { padding: 0.55rem 0; border-top: 1px solid var(--border); font-size: 0.95rem; }
.updates .when { display: block; font-family: var(--font-ui); font-size: 0.78rem; color: var(--muted); }
.empty { color: var(--muted); }

.site-footer { border-top: 1px solid var(--border); color: var(--muted); font-family: var(--font-ui); font-size: 0.82rem; }
.site-footer .wrap { max-width: 72rem; margin: 0 auto; padding: 1.25rem; }
.site-footer p { margin: 0; }

@media (max-width: 820px) {
  .reading { display: block; }
  .nav-button { display: inline-block; }
  .site-nav { margin-left: 0; width: 100%; display: none; }
  .nav-toggle:checked ~ .site-header .site-nav { display: block; }
  .chapters-nav { position: static; max-height: none; border-right: 0; border-bottom: 1px solid var(--border); }
  .reading main { padding: 1.75rem 1.1rem 3rem; }
  .story-head .cover { width: 8rem; }
}
@media print {
  .chapters-nav, .site-nav, .pager, .nav-button, .site-header, .site-footer { display: none !important; }
  .reading { display: block; }
  .reading main { padding: 0; }
}
"""

PAGE_MARK = "/* ============ PAGE SPECIFIC RULES ============ */"
PAGE_HELP = """/*
  Library page:            body.page-library
  Extra pages:             body.page-your-page-slug
  Every story page:        body.page-story
  Every chapter page:      body.page-chapter
  One story and chapters:  body.story-your-story-slug
  Genre listings:          body.page-genre
  Example: body.story-the-lighthouse .prose { --measure: 30rem; }
*/
"""


def css_stub(label, cls):
    return "\n/* --- %s (body.%s) --- */\nbody.%s {\n}\n" % (label.replace("*/", ""), cls, cls)


def default_css(theme="paperback"):
    return (
        CSS_HEADER + root_block(theme) + "\n" + BASE_CSS + "\n" + PAGE_MARK + "\n" + PAGE_HELP
        + css_stub("Library page", "page-library") + css_stub("All chapters", "page-chapter")
    )


# ----------------------------------------------------------------------------
# Seed content
# ----------------------------------------------------------------------------

HOME_MD = """Welcome. This is the library page. Edit it with **Library page** in the editor,
then add your first story.
"""

SAMPLE_MD = """The lamp had been out for three days before anyone on shore thought to ask why.

Mara climbed the stairs anyway, the way she had every evening since the spring, counting
the turns because counting kept her from listening. Ninety-two steps. At the top, the glass
was cold and clean and entirely dark.

* * *

She lit it herself, in the end, with a match and a great deal of swearing, and when the
beam swung out across the water something out there swung back.
"""

ABOUT_MD = """A line or two about you and what you write.

You can put contact details, a mailing list link, or a note about reposting here.
"""


# ----------------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------------

SITE_BOOLS = ("rss", "sitemap", "noindex", "dropcap", "wordcount", "reading_time", "updates")


def default_cfg(title):
    return {
        "version": 1,
        "site": {
            "title": title or "My stories",
            "tagline": "",
            "author": "",
            "footer": "&copy; {year} {author}. All rights reserved.",
            "lang": "en",
            "url": "",
            "description": "",
            "prose": "indented",
            "dropcap": True,
            "wordcount": True,
            "reading_time": True,
            "updates": True,
            "rss": False,
            "sitemap": False,
            "noindex": False,
            "wpm": 240,
        },
        "pages": [],
        "stories": [],
        "generated": [],
    }


def today():
    return datetime.date.today().isoformat()


def title_from_name(name):
    t = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", str(name))).strip()
    return (t[:1].upper() + t[1:]) if t else "Untitled"


def first_h1(text):
    for line in text.splitlines()[:30]:
        if not line.strip():
            continue
        m = re.match(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", line)
        return m.group(1).strip() if m else ""
    return ""


def strip_title_h1(text, title):
    """Drop a leading '# Title' line that repeats the chapter title, since the page adds its own."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = re.match(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", line)
        if m and m.group(1).strip().lower() == title.strip().lower():
            return "\n".join(lines[i + 1:])
        return text
    return text


def file_date(path):
    try:
        return datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return today()


def rename_path(src, dst):
    """Rename that also works for case-only changes on case-insensitive file systems."""
    if src == dst:
        return
    tmp = src.with_name(src.name + ".sg-rename")
    os.replace(src, tmp)
    os.replace(tmp, dst)


def plain_text(html_):
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", html_, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", _unescape(t)).strip()


def count_words(markdown):
    t = plain_text(Markdown("").render(str(markdown or "")))
    return len(re.findall(r"[^\s]+", t))


def nice_words(n):
    if n >= 1000:
        return "%s words" % ("{:,}".format(int(round(n / 100.0) * 100)))
    return "1 word" if n == 1 else "%d words" % n


def reading_time(words, wpm):
    mins = max(1, int(round(words / float(max(60, wpm)))))
    if mins < 60:
        return "%d min read" % mins
    h, m = divmod(mins, 60)
    return "%dh %02dm read" % (h, m)


def clean_genres(raw, known=None):
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    out, seen = [], {}
    for g in items:
        g = re.sub(r"\s+", " ", str(g)).strip(" ,")[:40]
        if not g or not slugify(g):
            continue
        key = slugify(g)
        if key in seen:
            continue
        seen[key] = True
        out.append(g)
    if known:
        for g in out:
            for k in known:
                if slugify(k) == slugify(g) and k != g:
                    out[out.index(g)] = k
    return out[:8]


def visible_stories(cfg):
    return [s for s in cfg["stories"] if not s.get("draft")]


def visible_chapters(story):
    return [c for c in story.get("chapters", []) if not c.get("draft")]


def genre_map(cfg):
    out = {}
    for s in visible_stories(cfg):
        for g in s.get("genres", []):
            out.setdefault(slugify(g), {"name": g, "stories": []})["stories"].append(s)
    return dict(sorted(out.items()))


def write_if_changed(path, text):
    if path.is_file() and read_text(path) == text:
        return
    write_text(path, text)


class Library:
    def __init__(self, root):
        self.root = root
        self.meta = root / META_DIR
        self.cfg = None

    # ---- paths
    def home_md(self):
        return self.meta / "home.md"

    def page_md(self, slug):
        return self.meta / "pages" / (slug + ".md")

    def story_dir(self, s):
        return self.meta / "stories" / s

    def story_md(self, s):
        return self.story_dir(s) / "_index.md"

    def chapter_md(self, s, c):
        return self.story_dir(s) / (c + ".md")

    def css_path(self):
        return self.root / "style.css"

    # ---- load and save
    def load(self):
        f = self.meta / "config.json"
        if f.exists():
            try:
                self.cfg = json.loads(f.read_text(encoding="utf-8"))
            except ValueError as e:
                raise ApiError("The settings file %s is not valid JSON (%s). Fix or remove it." % (f, e))
        else:
            self.cfg = default_cfg(title_from_name(self.root.name))
            write_text(self.home_md(), HOME_MD)
            write_text(self.page_md("about"), ABOUT_MD)
            self.cfg["pages"].append({"slug": "about", "title": "About", "description": ""})
            write_text(self.story_md("the-lighthouse"), SAMPLE_MD)
            self.cfg["stories"].append({
                "slug": "the-lighthouse", "title": "The Lighthouse", "subtitle": "", "author": "",
                "status": "complete", "kind": "single", "blurb": "A sample short story. Delete it when you add your own.",
                "cover": "", "genres": ["Sample"], "warnings": "", "series": "", "series_no": "",
                "started": today(), "completed": today(), "draft": False, "description": "", "chapters": [],
            })
        (self.root / "assets").mkdir(parents=True, exist_ok=True)
        if not self.css_path().exists():
            write_text(self.css_path(), default_css("paperback"))
        self.normalize()
        self.rescan()
        self.save_cfg()
        self.build()

    def save_cfg(self):
        write_text(self.meta / "config.json", json.dumps(self.cfg, indent=2, ensure_ascii=False) + "\n")

    def normalize(self):
        c = self.cfg if isinstance(self.cfg, dict) else {}
        self.cfg = c
        d = default_cfg("")
        if not isinstance(c.get("site"), dict):
            c["site"] = {}
        for k, v in d["site"].items():
            c["site"].setdefault(k, v)
        if c["site"].get("prose") not in ("indented", "spaced"):
            c["site"]["prose"] = "indented"
        try:
            c["site"]["wpm"] = max(60, min(1000, int(c["site"].get("wpm") or 240)))
        except (TypeError, ValueError):
            c["site"]["wpm"] = 240
        if not isinstance(c.get("generated"), list):
            c["generated"] = []
        pages, seen = [], set()
        for p in c.get("pages") if isinstance(c.get("pages"), list) else []:
            if not isinstance(p, dict) or not slugify(p.get("slug", "")) or p["slug"] in seen:
                continue
            p["slug"] = slugify(p["slug"])
            seen.add(p["slug"])
            p["title"] = str(p.get("title") or title_from_name(p["slug"]))
            p["description"] = str(p.get("description") or "")
            pages.append(p)
        c["pages"] = pages
        stories, sseen = [], set()
        for s in c.get("stories") if isinstance(c.get("stories"), list) else []:
            if not isinstance(s, dict) or not slugify(s.get("slug", "")) or s["slug"] in sseen:
                continue
            s["slug"] = slugify(s["slug"])
            sseen.add(s["slug"])
            s["title"] = str(s.get("title") or title_from_name(s["slug"]))
            for k in ("subtitle", "author", "blurb", "cover", "warnings", "series", "series_no", "description"):
                s[k] = str(s.get(k) or "")
            s["status"] = s.get("status") if s.get("status") in STATUS_LABELS else "ongoing"
            s["kind"] = "single" if s.get("kind") == "single" else "chaptered"
            s["genres"] = clean_genres(s.get("genres"))
            s["draft"] = bool(s.get("draft"))
            s["started"] = clean_date(s.get("started"))
            s["completed"] = str(s.get("completed") or "")
            chs, cseen = [], set()
            for ch in s.get("chapters") if isinstance(s.get("chapters"), list) else []:
                if not isinstance(ch, dict) or not slugify(ch.get("slug", "")) or ch["slug"] in cseen:
                    continue
                cseen.add(ch["slug"])
                ch["title"] = str(ch.get("title") or title_from_name(ch["slug"]))
                ch["note"] = str(ch.get("note") or "")
                ch["draft"] = bool(ch.get("draft"))
                ch["date"] = clean_date(ch.get("date"))
                ch["words"] = int(ch.get("words") or 0)
                chs.append(ch)
            s["chapters"] = chs
            stories.append(s)
        c["stories"] = stories

    def state(self):
        return {
            "folder": str(self.root),
            "site": self.cfg["site"],
            "pages": self.cfg["pages"],
            "stories": self.cfg["stories"],
            "statuses": [{"id": k, "label": v} for k, v in STATUSES],
            "genres": sorted({g for s in self.cfg["stories"] for g in s.get("genres", [])}, key=str.lower),
            "themes": [{"id": k, "label": v[0], "block": root_block(k)} for k, v in THEMES.items()],
        }

    # ---- lookups
    def find_story(self, slug, cfg=None):
        return next((s for s in (cfg or self.cfg)["stories"] if s["slug"] == slug), None)

    def get_story(self, slug):
        s = self.find_story(slug)
        if not s:
            raise ApiError("That story does not exist.", 404)
        return s

    def get_chapter(self, sslug, cslug):
        s = self.get_story(sslug)
        c = next((x for x in s["chapters"] if x["slug"] == cslug), None)
        if not c:
            raise ApiError("That chapter does not exist.", 404)
        return s, c

    def find_page(self, slug):
        return next((p for p in self.cfg["pages"] if p["slug"] == slug), None)

    def unique_story_slug(self, base, exclude=None):
        base = slugify(base) or "story"
        taken = ({s["slug"] for s in self.cfg["stories"] if s["slug"] != exclude}
                 | {p["slug"] for p in self.cfg["pages"]} | RESERVED_SLUGS)
        slug, n = base, 2
        while slug in taken or (slug != exclude and (self.meta / "stories" / slug).exists()):
            slug = "%s-%d" % (base, n)
            n += 1
        return slug

    def unique_page_slug(self, base, exclude=None):
        base = slugify(base) or "page"
        taken = ({p["slug"] for p in self.cfg["pages"] if p["slug"] != exclude}
                 | {s["slug"] for s in self.cfg["stories"]} | RESERVED_SLUGS)
        slug, n = base, 2
        while slug in taken:
            slug = "%s-%d" % (base, n)
            n += 1
        return slug

    def unique_chapter_slug(self, story, base, exclude=None):
        base = slugify(base) or "chapter"
        taken = {c["slug"] for c in story["chapters"] if c["slug"] != exclude} | {"index", "_index"}
        slug, n = base, 2
        while slug in taken or (slug != exclude and self.chapter_md(story["slug"], slug).exists()):
            slug = "%s-%d" % (base, n)
            n += 1
        return slug

    def to_trash(self, path):
        if not path.exists():
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.meta / "trash" / ("%s-%s" % (stamp, path.name))
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 2
        while dest.exists():
            dest = dest.with_name("%s-%s-%d" % (stamp, path.name, n))
            n += 1
        shutil.move(str(path), str(dest))

    # ---- rescan
    def rescan(self):
        base = self.meta / "stories"
        base.mkdir(parents=True, exist_ok=True)
        result = {"stories_added": 0, "chapters_added": 0, "chapters_removed": 0}
        known = {s["slug"] for s in self.cfg["stories"]}
        for d in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir() or d.name.startswith(".") or d.name in known:
                continue
            slug = self.unique_story_slug(d.name, exclude=d.name if slugify(d.name) == d.name else None)
            if slug != d.name:
                if (base / slug).exists() and not (base / slug).samefile(d):
                    continue
                rename_path(d, base / slug)
            self.cfg["stories"].append({
                "slug": slug, "title": title_from_name(d.name), "subtitle": "", "author": "", "status": "ongoing",
                "kind": "chaptered", "blurb": "", "cover": "", "genres": [], "warnings": "", "series": "",
                "series_no": "", "started": today(), "completed": "", "draft": False, "description": "", "chapters": [],
            })
            known.add(slug)
            result["stories_added"] += 1

        for st in self.cfg["stories"]:
            d = self.story_dir(st["slug"])
            d.mkdir(parents=True, exist_ok=True)
            chs = {c["slug"]: c for c in st["chapters"]}
            for f in sorted(d.glob("*.md"), key=lambda p: p.name.lower()):
                if f.name == "_index.md" or f.name.startswith("."):
                    continue
                if f.stem in chs:
                    continue
                orig = f.stem
                slug = orig if slugify(orig) == orig and orig not in ("index", "_index") else None
                if slug is None:
                    slug = self.unique_chapter_slug(st, orig)
                    target = self.chapter_md(st["slug"], slug)
                    if target.exists() and not target.samefile(f):
                        continue
                    rename_path(f, target)
                    f = target
                ch = {"slug": slug, "title": first_h1(read_text(f)) or title_from_name(orig), "date": file_date(f),
                      "draft": False, "note": "", "words": 0}
                st["chapters"].append(ch)
                chs[slug] = ch
                result["chapters_added"] += 1
            keep = [c for c in st["chapters"] if self.chapter_md(st["slug"], c["slug"]).is_file()]
            result["chapters_removed"] += len(st["chapters"]) - len(keep)
            st["chapters"] = keep
            if st["chapters"] and st["kind"] == "single":
                st["kind"] = "chaptered"
        return result

    def api_rescan(self):
        r = self.rescan()
        self.save_cfg()
        self.build()
        return r

    # ---- rendering helpers
    def body_classes(self, cfg, extra):
        site = cfg["site"]
        cls = ["prose-" + site.get("prose", "indented")] + extra
        if site.get("dropcap"):
            cls.append("dropcap")
        return " ".join(cls)

    def nav_html(self, cfg, prefix, current):
        items = ['<li><a href="%sindex.html"%s>Library</a></li>' % (prefix, ' aria-current="page"' if current == "library" else "")]
        for p in cfg["pages"]:
            items.append('<li><a href="%s%s.html"%s>%s</a></li>'
                         % (prefix, p["slug"], ' aria-current="page"' if current == p["slug"] else "", esc(p["title"])))
        return '<nav class="site-nav" aria-label="Main"><ul>%s</ul></nav>' % "".join(items)

    def story_facts(self, cfg, story, prefix, words=None, chapters=None):
        site = cfg["site"]
        out = ['<li><span class="tag status">%s</span></li>' % esc(STATUS_LABELS[story["status"]])]
        for g in story.get("genres", []):
            out.append('<li><a class="tag" href="%sgenre/%s.html">%s</a></li>' % (prefix, slugify(g), esc(g)))
        if chapters is not None and story["kind"] == "chaptered":
            out.append("<li>%d chapter%s</li>" % (chapters, "" if chapters == 1 else "s"))
        if words and site.get("wordcount"):
            out.append("<li>%s</li>" % esc(nice_words(words)))
        if words and site.get("reading_time"):
            out.append("<li>%s</li>" % esc(reading_time(words, site.get("wpm", 240))))
        if story.get("series"):
            label = story["series"] + (" #%s" % story["series_no"] if story.get("series_no") else "")
            out.append("<li>%s</li>" % esc(label))
        return '<ul class="facts">%s</ul>' % "".join(out)

    def chapters_nav(self, cfg, story, prefix, current=None):
        chs = visible_chapters(story)
        author = story.get("author") or cfg["site"].get("author") or ""
        items = []
        for c in chs:
            items.append('<li><a href="%s.html"%s>%s</a></li>'
                         % (c["slug"], ' aria-current="page"' if c["slug"] == current else "", esc(c["title"])))
        return (
            '<nav class="chapters-nav" aria-label="Chapters">\n'
            '<a class="in-story" href="index.html">%s</a>%s\n<ol>\n%s\n</ol>\n'
            '<a class="all-stories" href="%sindex.html">All stories</a>\n</nav>'
            % (esc(story["title"]), '<span class="by">%s</span>' % esc(author) if author else "",
               "\n".join(items) or '<li class="empty">No chapters yet.</li>', prefix)
        )

    def doc(self, cfg, *, prefix, body_class, title, desc, rel, main_html, current="", css=None, base=None,
            og_type="website", when="", image=""):
        site = cfg["site"]
        st = site["title"]
        full = st if not title or title == st else "%s | %s" % (title, st)
        desc = re.sub(r"\s+", " ", (desc or "")).strip()[:300]
        canonical = abs_url(site.get("url"), rel)
        img = resolve_img(site, image)
        head = ['<meta charset="utf-8">']
        if base:
            head.append('<base href="%s">' % attr(base))
        head.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        head.append("<title>%s</title>" % esc(full))
        if desc:
            head.append('<meta name="description" content="%s">' % attr(desc))
        if site.get("author"):
            head.append('<meta name="author" content="%s">' % attr(site["author"]))
        if canonical:
            head.append('<link rel="canonical" href="%s">' % attr(canonical))
        if site.get("noindex"):
            head.append('<meta name="robots" content="noindex, follow">')
        head.append('<meta property="og:type" content="%s">' % og_type)
        head.append('<meta property="og:title" content="%s">' % attr(title or st))
        head.append('<meta property="og:site_name" content="%s">' % attr(st))
        if desc:
            head.append('<meta property="og:description" content="%s">' % attr(desc))
        if canonical:
            head.append('<meta property="og:url" content="%s">' % attr(canonical))
        if img:
            head.append('<meta property="og:image" content="%s">' % attr(img))
        if og_type == "article" and when:
            head.append('<meta property="article:published_time" content="%s">' % attr(when))
        head.append('<meta name="twitter:card" content="%s">' % ("summary_large_image" if img else "summary"))
        if site.get("rss") and site.get("url"):
            head.append('<link rel="alternate" type="application/rss+xml" title="%s" href="%srss">' % (attr(st), prefix))
        if css is None:
            head.append('<link rel="stylesheet" href="%sstyle.css">' % prefix)
        else:
            head.append("<style>\n%s\n</style>" % css.replace("</", "<\\/"))
        tagline = '\n<span class="tagline">%s</span>' % esc(site["tagline"]) if site.get("tagline") else ""
        foot = (site.get("footer", "").replace("{year}", str(datetime.date.today().year))
                .replace("{title}", st).replace("{author}", site.get("author") or st))
        footer = ""
        if foot.strip():
            footer = '\n<footer class="site-footer"><div class="wrap"><p>%s</p></div></footer>' % Markdown(prefix).inline(foot)
        return (
            '<!DOCTYPE html>\n<html lang="%s">\n<head>\n%s\n</head>\n<body class="%s">\n'
            '<a class="skip" href="#read">Skip to content</a>\n'
            '<input type="checkbox" id="nav-toggle" class="nav-toggle" aria-label="Show menu">\n'
            '<header class="site-header"><div class="wrap">\n'
            '<a class="site-title" href="%sindex.html">%s</a>%s\n'
            '<label for="nav-toggle" class="nav-button">Menu</label>\n%s\n</div></header>\n%s%s\n</body>\n</html>\n'
            % (attr(site.get("lang") or "en"), "\n".join(head), attr(body_class), prefix, esc(st), tagline,
               self.nav_html(cfg, prefix, current), main_html, footer)
        )

    # ---- pages
    def render_library(self, cfg, markdown, css=None, base=None):
        site = cfg["site"]
        M = Markdown("")
        intro = M.render(markdown)
        stories = visible_stories(cfg)
        cards = []
        for s in stories:
            words = self.story_words(cfg, s)
            src = self.cover_src(s, "")
            cover = ('<img class="cover" src="%s" alt="Cover of %s" loading="lazy">' % (attr(src), attr(s["title"]))
                     if src else '<span class="cover none">%s</span>' % esc(s["title"]))
            blurb = '<p>%s</p>' % esc(s["blurb"]) if s.get("blurb") else ""
            sub = '<p class="subtitle">%s</p>' % esc(s["subtitle"]) if s.get("subtitle") else ""
            cards.append(
                '<article class="book">\n<a href="%s/index.html" tabindex="-1" aria-hidden="true">%s</a>\n<div>\n'
                '<h2><a href="%s/index.html">%s</a></h2>\n%s%s%s</div>\n</article>'
                % (s["slug"], cover, s["slug"], esc(s["title"]), sub, blurb,
                   self.story_facts(cfg, s, "", words, len(visible_chapters(s))))
            )
        parts = ['<div class="plain" id="read">']
        if intro:
            parts.append('<div class="prose library-intro">%s</div>' % intro)
        if cards:
            parts.append('<h2 class="section-title">Stories</h2>\n<div class="shelf">\n%s\n</div>' % "\n".join(cards))
        else:
            parts.append('<p class="empty">No stories yet.</p>')
        if site.get("updates"):
            recent = self.recent_chapters(cfg, 6)
            if recent:
                lis = []
                for s, c in recent:
                    lis.append('<li><a href="%s/%s.html">%s</a><span class="when">%s, %s</span></li>'
                               % (s["slug"], c["slug"], esc(c["title"]), esc(s["title"]), esc(nice_date(c["date"]))))
                parts.append('<h2 class="section-title">Latest chapters</h2>\n<ul class="updates">\n%s\n</ul>' % "\n".join(lis))
        parts.append("</div>")
        desc = site.get("description") or site.get("tagline") or auto_summary(intro)
        return self.doc(cfg, prefix="", body_class=self.body_classes(cfg, ["page-library"]), title="", desc=desc,
                        rel="index.html", main_html="\n".join(parts), current="library", css=css, base=base)

    def render_page(self, cfg, page, markdown, css=None, base=None):
        M = Markdown("")
        body = M.render(markdown)
        main = ('<div class="plain" id="read">\n<article class="prose page-body">\n<h1>%s</h1>\n%s\n</article>\n</div>'
                % (esc(page["title"]), body))
        desc = page.get("description") or auto_summary(body) or cfg["site"].get("description", "")
        return self.doc(cfg, prefix="", body_class=self.body_classes(cfg, ["page-" + page["slug"]]),
                        title=page["title"], desc=desc, rel=page["slug"] + ".html", main_html=main,
                        current=page["slug"], css=css, base=base)

    @staticmethod
    def cover_src(story, prefix):
        """The cover as a usable URL, or empty when it is missing or not a safe image path."""
        url = fix_url(story.get("cover", ""), prefix) if story.get("cover") else ""
        return "" if url in ("", "#") else url

    def story_words(self, cfg, story):
        if story["kind"] == "single":
            return story.get("words", 0)
        return sum(c.get("words", 0) for c in visible_chapters(story))

    def recent_chapters(self, cfg, limit):
        rows = [(s, c) for s in visible_stories(cfg) for c in visible_chapters(s)]
        rows.sort(key=lambda x: (x[1]["date"], x[1]["title"]), reverse=True)
        return rows[:limit]

    def render_story(self, cfg, story, markdown, css=None, base=None):
        site = cfg["site"]
        prefix = "../"
        M = Markdown(prefix)
        body = M.render(markdown)
        chs = visible_chapters(story)
        words = story.get("words", 0) if story["kind"] == "single" else sum(c.get("words", 0) for c in chs)
        author = story.get("author") or site.get("author") or ""
        head = ['<div class="story-head">']
        cover_src = self.cover_src(story, prefix)
        if cover_src:
            head.append('<img class="cover" src="%s" alt="Cover of %s">' % (attr(cover_src), attr(story["title"])))
        head.append('<div class="about">\n<h1>%s</h1>' % esc(story["title"]))
        if story.get("subtitle"):
            head.append('<p class="subtitle">%s</p>' % esc(story["subtitle"]))
        if author:
            head.append('<p class="byline">by %s</p>' % esc(author))
        head.append(self.story_facts(cfg, story, prefix, words, len(chs)))
        if story.get("blurb"):
            head.append('<p class="blurb">%s</p>' % esc(story["blurb"]))
        if story.get("warnings"):
            head.append('<p class="warnings"><strong>Content notes:</strong> %s</p>' % esc(story["warnings"]))
        if story["kind"] == "chaptered" and chs:
            head.append('<p><a class="read-button" href="%s.html">Start reading</a></p>' % attr(chs[0]["slug"]))
        head.append("</div>\n</div>")

        parts = list(head)
        if story["kind"] == "single":
            if body:
                parts.append('<article class="prose opening">\n%s\n</article>' % body)
            if story["status"] == "complete" and body:
                parts.append('<p class="the-end">The end</p>')
        else:
            if body:
                parts.append('<div class="prose foreword">%s</div>' % body)
            if chs:
                lis = []
                for c in chs:
                    meta = []
                    if site.get("wordcount") and c.get("words"):
                        meta.append(nice_words(c["words"]))
                    meta.append(nice_date(c["date"]))
                    lis.append('<li><a href="%s.html"><span class="ch-title">%s</span><span class="ch-meta">%s</span></a></li>'
                               % (attr(c["slug"]), esc(c["title"]), esc(" \u00b7 ".join(meta))))
                parts.append('<section class="toc">\n<h2>Chapters</h2>\n<ol>\n%s\n</ol>\n</section>' % "\n".join(lis))
            else:
                parts.append('<p class="empty">No chapters published yet.</p>')

        desc = story.get("description") or story.get("blurb") or auto_summary(body)
        cls = self.body_classes(cfg, ["page-story", "story-" + story["slug"]])
        inner = '<div class="sheet">\n%s\n</div>' % "\n".join(parts)
        if story["kind"] == "chaptered":
            main = ('<div class="reading">\n%s\n<main id="read">\n%s\n</main>\n</div>'
                    % (self.chapters_nav(cfg, story, prefix), inner))
        else:
            main = '<div class="plain" id="read">\n%s\n</div>' % inner
        return self.doc(cfg, prefix=prefix, body_class=cls, title=story["title"], desc=desc,
                        rel="%s/index.html" % story["slug"], main_html=main, css=css, base=base,
                        og_type="article", image=story.get("cover", ""))

    def render_chapter(self, cfg, story, chapter, markdown, css=None, base=None):
        site = cfg["site"]
        prefix = "../"
        M = Markdown(prefix)
        body = M.render(strip_title_h1(markdown, chapter["title"]))
        chs = visible_chapters(story)
        slugs = [c["slug"] for c in chs]
        n = slugs.index(chapter["slug"]) + 1 if chapter["slug"] in slugs else 0
        meta = []
        if n:
            meta.append("Chapter %d of %d" % (n, len(chs)))
        if site.get("wordcount") and chapter.get("words"):
            meta.append(nice_words(chapter["words"]))
        if site.get("reading_time") and chapter.get("words"):
            meta.append(reading_time(chapter["words"], site.get("wpm", 240)))
        meta.append(nice_date(chapter["date"]))
        parts = [
            '<header class="chapter-head">\n<span class="in"><a href="index.html">%s</a></span>\n<h1>%s</h1>\n'
            '<p class="meta">%s</p>\n</header>' % (esc(story["title"]), esc(chapter["title"]), esc(" \u00b7 ".join(meta))),
            '<article class="prose opening">\n%s\n</article>' % body,
        ]
        if chapter.get("note"):
            parts.append('<aside class="note prose">\n<h2>Author\u2019s note</h2>\n%s\n</aside>'
                         % Markdown(prefix).render(chapter["note"]))
        if n and n == len(chs) and story["status"] == "complete":
            parts.append('<p class="the-end">The end</p>')
        pager = []
        if n > 1:
            p = chs[n - 2]
            pager.append('<a class="prev" href="%s.html"><small>Previous</small>%s</a>' % (attr(p["slug"]), esc(p["title"])))
        if n and n < len(chs):
            x = chs[n]
            pager.append('<a class="next" href="%s.html"><small>Next</small>%s</a>' % (attr(x["slug"]), esc(x["title"])))
        if pager:
            parts.append('<nav class="pager" aria-label="Chapters">%s</nav>' % "".join(pager))
        main = ('<div class="reading">\n%s\n<main id="read">\n<div class="sheet">\n%s\n</div>\n</main>\n</div>'
                % (self.chapters_nav(cfg, story, prefix, chapter["slug"]), "\n".join(parts)))
        desc = auto_summary(body) or story.get("blurb", "")
        cls = self.body_classes(cfg, ["page-chapter", "story-" + story["slug"], "chapter-" + chapter["slug"]])
        return self.doc(cfg, prefix=prefix, body_class=cls, title="%s | %s" % (chapter["title"], story["title"]),
                        desc=desc, rel="%s/%s.html" % (story["slug"], chapter["slug"]), main_html=main, css=css,
                        base=base, og_type="article", when=chapter["date"], image=story.get("cover", ""))

    def render_genre(self, cfg, name, stories, css=None, base=None):
        prefix = "../"
        cards = []
        for s in stories:
            src = self.cover_src(s, prefix)
            cover = ('<img class="cover" src="%s" alt="Cover of %s" loading="lazy">' % (attr(src), attr(s["title"]))
                     if src else '<span class="cover none">%s</span>' % esc(s["title"]))
            blurb = '<p>%s</p>' % esc(s["blurb"]) if s.get("blurb") else ""
            cards.append('<article class="book">\n<a href="%s../%s/index.html" tabindex="-1" aria-hidden="true">%s</a>\n<div>\n'
                         '<h2><a href="../%s/index.html">%s</a></h2>\n%s%s</div>\n</article>'
                         % ("", s["slug"], cover, s["slug"], esc(s["title"]), blurb,
                            self.story_facts(cfg, s, prefix, self.story_words(cfg, s), len(visible_chapters(s)))))
        main = ('<div class="plain" id="read">\n<h1 class="page-body">%s</h1>\n<div class="shelf">\n%s\n</div>\n'
                '<p><a href="%sindex.html">All stories</a></p>\n</div>'
                % (esc(name), "\n".join(cards), prefix))
        return self.doc(cfg, prefix=prefix, body_class=self.body_classes(cfg, ["page-genre", "genre-" + slugify(name)]),
                        title=name, desc="Stories tagged %s." % name, rel="genre/%s.html" % slugify(name),
                        main_html=main, css=css, base=base)

    # ---- RSS
    def rss_xml(self, cfg):
        site = cfg["site"]
        url = site["url"].rstrip("/")
        items = []
        for s, c in self.recent_chapters(cfg, 50):
            link = "%s/%s/%s.html" % (url, s["slug"], c["slug"])
            body = Markdown("").render(read_text(self.chapter_md(s["slug"], c["slug"])))
            when = email.utils.format_datetime(
                datetime.datetime.combine(datetime.date.fromisoformat(c["date"]), datetime.time(12, 0),
                                          tzinfo=datetime.timezone.utc))
            items.append(
                "<item>\n<title>%s</title>\n<link>%s</link>\n<guid isPermaLink=\"true\">%s</guid>\n"
                "<pubDate>%s</pubDate>\n<category>%s</category>\n<description>%s</description>\n"
                "<content:encoded><![CDATA[%s]]></content:encoded>\n</item>"
                % (_escape("%s: %s" % (s["title"], c["title"]), quote=False), _escape(link, quote=False),
                   _escape(link, quote=False), when, _escape(s["title"], quote=False),
                   _escape(auto_summary(body), quote=False), body.replace("]]>", "]]&gt;"))
            )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
            'xmlns:atom="http://www.w3.org/2005/Atom">\n<channel>\n'
            "<title>%s</title>\n<link>%s/index.html</link>\n<description>%s</description>\n"
            '<language>%s</language>\n<generator>StoryGen</generator>\n'
            '<atom:link href="%s/rss" rel="self" type="application/rss+xml"/>\n%s\n</channel>\n</rss>\n'
            % (_escape(site["title"], quote=False), _escape(url, quote=False),
               _escape(site.get("description") or site.get("tagline") or site["title"], quote=False),
               _escape(site.get("lang") or "en", quote=False), _escape(url, quote=False), "\n".join(items))
        )

    def sitemap_xml(self):
        c = self.cfg
        url = c["site"]["url"].rstrip("/")
        rows = ["<url><loc>%s/index.html</loc></url>" % _escape(url, quote=False)]
        for p in c["pages"]:
            rows.append("<url><loc>%s</loc></url>" % _escape("%s/%s.html" % (url, p["slug"]), quote=False))
        for s in visible_stories(c):
            rows.append("<url><loc>%s</loc></url>" % _escape("%s/%s/index.html" % (url, s["slug"]), quote=False))
            for ch in visible_chapters(s):
                rows.append("<url><loc>%s</loc><lastmod>%s</lastmod></url>"
                            % (_escape("%s/%s/%s.html" % (url, s["slug"], ch["slug"]), quote=False), ch["date"]))
        for slug in genre_map(c):
            rows.append("<url><loc>%s</loc></url>" % _escape("%s/genre/%s.html" % (url, slug), quote=False))
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!-- generated by StoryGen -->\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n%s\n</urlset>\n' % "\n".join(rows))

    def write_seo_files(self):
        site = self.cfg["site"]
        url = (site.get("url") or "").rstrip("/")
        sm, rb = self.root / "sitemap.xml", self.root / "robots.txt"

        def ours(path):
            return path.is_file() and "generated by StoryGen" in read_text(path)[:300]

        want = bool(site.get("sitemap")) and bool(url) and not site.get("noindex")
        if want and not sm.is_dir() and (not sm.exists() or ours(sm)):
            write_if_changed(sm, self.sitemap_xml())
        elif not want and ours(sm):
            sm.unlink()
        if url and site.get("noindex"):
            robots = "# generated by StoryGen\nUser-agent: *\nDisallow: /\n"
        elif want:
            robots = "# generated by StoryGen\nUser-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % url
        else:
            robots = None
        if robots and not rb.is_dir() and (not rb.exists() or ours(rb)):
            write_if_changed(rb, robots)
        elif robots is None and ours(rb):
            rb.unlink()

    # ---- build
    def build(self):
        c = self.cfg
        site = c["site"]
        for s in c["stories"]:
            if s["kind"] == "single":
                s["words"] = count_words(read_text(self.story_md(s["slug"])))
            else:
                s.pop("words", None)
                for ch in s["chapters"]:
                    ch["words"] = count_words(read_text(self.chapter_md(s["slug"], ch["slug"])))
        files = {"index.html": self.render_library(c, read_text(self.home_md()))}
        for p in c["pages"]:
            files[p["slug"] + ".html"] = self.render_page(c, p, read_text(self.page_md(p["slug"])))
        for s in visible_stories(c):
            files["%s/index.html" % s["slug"]] = self.render_story(c, s, read_text(self.story_md(s["slug"])))
            for ch in visible_chapters(s):
                files["%s/%s.html" % (s["slug"], ch["slug"])] = self.render_chapter(
                    c, s, ch, read_text(self.chapter_md(s["slug"], ch["slug"])))
        for slug, g in genre_map(c).items():
            files["genre/%s.html" % slug] = self.render_genre(c, g["name"], g["stories"])
        if site.get("rss") and site.get("url"):
            xml = self.rss_xml(c)
            files["rss"] = xml
            files["rss.xml"] = xml
        for rel, text in files.items():
            write_if_changed(self.root / rel, text)
        for rel in set(c.get("generated", [])) - set(files):
            self.remove_generated(rel)
        c["generated"] = sorted(files)
        self.write_seo_files()
        self.save_cfg()

    def remove_generated(self, rel):
        """Delete a file this app generated earlier and no longer produces. Paths are checked so a
        tampered config can never reach outside the library folder or into the markdown sources."""
        try:
            p = (self.root / str(rel)).resolve()
            parts = p.relative_to(self.root.resolve()).parts
        except (ValueError, OSError):
            return
        if not parts or parts[0].startswith(".") or parts[0] == "assets":
            return
        if p.suffix not in (".html", ".xml") and p.name != "rss":
            return
        if p.is_file():
            p.unlink()
        parent = p.parent
        if parent != self.root.resolve():
            try:
                parent.rmdir()
            except OSError:
                pass

    # ---- css
    def add_css_stub(self, label, cls):
        css = read_text(self.css_path())
        if ("body.%s " % cls) not in css and ("body.%s{" % cls) not in css:
            write_text(self.css_path(), css.rstrip("\n") + "\n" + css_stub(label, cls))

    def get_css(self):
        return {"css": read_text(self.css_path())}

    def save_css(self, css):
        write_text(self.css_path(), str(css))
        return {"ok": True}

    # ---- home and pages
    def get_home(self):
        return {"markdown": read_text(self.home_md())}

    def save_home(self, d):
        write_text(self.home_md(), str(d.get("markdown") or ""))
        self.build()
        return {"ok": True}

    def add_page(self, title):
        title = str(title or "").strip()[:120]
        if not title:
            raise ApiError("Give the page a title.")
        slug = self.unique_page_slug(title)
        self.cfg["pages"].append({"slug": slug, "title": title, "description": ""})
        write_text(self.page_md(slug), "")
        self.add_css_stub("Page: " + title, "page-" + slug)
        self.save_cfg()
        self.build()
        return {"slug": slug}

    def get_page(self, slug):
        p = self.find_page(slug)
        if not p:
            raise ApiError("That page does not exist.", 404)
        return {"page": p, "markdown": read_text(self.page_md(slug))}

    def save_page(self, slug, d):
        p = self.find_page(slug)
        if not p:
            raise ApiError("That page does not exist.", 404)
        p["title"] = str(d.get("title") or "").strip()[:120] or p["title"]
        p["description"] = str(d.get("description") or "").strip()[:320]
        wanted = slugify(str(d.get("new_slug") or "")) or p["slug"]
        if wanted != p["slug"]:
            new = self.unique_page_slug(wanted, exclude=p["slug"])
            if new != wanted:
                raise ApiError("Another page or story already uses the address %s." % wanted)
            rename_path(self.page_md(p["slug"]), self.page_md(new))
            p["slug"] = new
        write_text(self.page_md(p["slug"]), str(d.get("markdown") or ""))
        self.save_cfg()
        self.build()
        return {"slug": p["slug"]}

    def delete_page(self, slug):
        p = self.find_page(slug)
        if not p:
            raise ApiError("That page does not exist.", 404)
        self.cfg["pages"].remove(p)
        self.to_trash(self.page_md(slug))
        self.save_cfg()
        self.build()
        return {"ok": True}

    # ---- stories
    def add_story(self, title, kind="chaptered"):
        title = str(title or "").strip()[:160]
        if not title:
            raise ApiError("Give the story a title.")
        slug = self.unique_story_slug(title)
        story = {
            "slug": slug, "title": title, "subtitle": "", "author": "", "status": "ongoing",
            "kind": "single" if kind == "single" else "chaptered", "blurb": "", "cover": "", "genres": [],
            "warnings": "", "series": "", "series_no": "", "started": today(), "completed": "", "draft": False,
            "description": "", "chapters": [],
        }
        self.cfg["stories"].append(story)
        write_text(self.story_md(slug), "")
        self.add_css_stub("Story: " + title, "story-" + slug)
        self.save_cfg()
        self.build()
        return {"slug": slug}

    def get_story_api(self, slug):
        s = self.get_story(slug)
        return {"story": s, "markdown": read_text(self.story_md(slug))}

    def save_story(self, slug, d):
        s = self.get_story(slug)
        s["title"] = str(d.get("title") or "").strip()[:160] or s["title"]
        for k, limit in (("subtitle", 200), ("author", 120), ("blurb", 600), ("cover", 400),
                         ("warnings", 400), ("series", 120), ("series_no", 12), ("description", 320)):
            if k in d:
                s[k] = str(d.get(k) or "").strip()[:limit]
        if d.get("status") in STATUS_LABELS:
            s["status"] = d["status"]
        if d.get("kind") in ("single", "chaptered"):
            if d["kind"] == "single" and s["chapters"]:
                raise ApiError("This story has chapters, so it cannot become a single piece. "
                               "Delete the chapters first, or leave it as chaptered.")
            s["kind"] = d["kind"]
        if "genres" in d:
            known = {g for st in self.cfg["stories"] for g in st.get("genres", [])}
            s["genres"] = clean_genres(d.get("genres"), known)
        if "draft" in d:
            s["draft"] = bool(d.get("draft"))
        if "started" in d:
            s["started"] = clean_date(d.get("started"))
        if "completed" in d:
            done = str(d.get("completed") or "").strip()
            s["completed"] = clean_date(done) if done else ""
        wanted = slugify(str(d.get("new_slug") or "")) or s["slug"]
        if wanted != s["slug"]:
            new = self.unique_story_slug(wanted, exclude=s["slug"])
            if new != wanted:
                raise ApiError("Another story or page already uses the address %s." % wanted)
            rename_path(self.story_dir(s["slug"]), self.story_dir(new))
            s["slug"] = new
        write_text(self.story_md(s["slug"]), str(d.get("markdown") or ""))
        self.save_cfg()
        self.build()
        return {"slug": s["slug"]}

    def delete_story(self, slug):
        s = self.get_story(slug)
        self.cfg["stories"].remove(s)
        self.to_trash(self.story_dir(slug))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def move_story(self, slug, direction):
        stories = self.cfg["stories"]
        s = self.get_story(slug)
        i = stories.index(s)
        j = max(0, min(len(stories) - 1, i + (1 if int(direction) > 0 else -1)))
        stories.insert(j, stories.pop(i))
        self.save_cfg()
        self.build()
        return {"ok": True}

    def order_chapters(self, slug, order):
        s = self.get_story(slug)
        by = {c["slug"]: c for c in s["chapters"]}
        new = [by.pop(x) for x in (order if isinstance(order, list) else []) if x in by]
        s["chapters"] = new + list(by.values())
        self.save_cfg()
        self.build()
        return {"ok": True}

    # ---- chapters
    def get_chapter_api(self, sslug, cslug):
        s, c = self.get_chapter(sslug, cslug)
        return {"story": s["slug"], "chapter": c, "markdown": read_text(self.chapter_md(s["slug"], c["slug"]))}

    def save_chapter(self, d):
        title = str(d.get("title") or "").strip()[:160]
        if not title:
            raise ApiError("Give the chapter a title.")
        target = self.find_story(str(d.get("story") or ""))
        if not target:
            raise ApiError("Pick a story for this chapter.")
        if target["kind"] == "single":
            raise ApiError("%s is set up as a single piece. Change it to chaptered in the story editor first."
                           % target["title"])
        md = str(d.get("markdown") or "")
        wanted = slugify(str(d.get("new_slug") or "")) or slugify(title) or "chapter"
        orig_story, orig_slug = str(d.get("orig_story") or ""), str(d.get("orig_slug") or "")
        if orig_slug:
            ostory, ch = self.get_chapter(orig_story, orig_slug)
            old_path = self.chapter_md(ostory["slug"], ch["slug"])
            same = ostory is target
            if not same or wanted != ch["slug"]:
                new_slug = self.unique_chapter_slug(target, wanted, exclude=ch["slug"] if same else None)
                if not same:
                    ostory["chapters"].remove(ch)
                    target["chapters"].append(ch)
                ch["slug"] = new_slug
            new_path = self.chapter_md(target["slug"], ch["slug"])
            write_text(new_path, md)
            if old_path != new_path:
                old_path.unlink(missing_ok=True)
        else:
            ch = {"slug": self.unique_chapter_slug(target, wanted), "date": today(), "draft": False, "note": "", "words": 0}
            target["chapters"].append(ch)
            write_text(self.chapter_md(target["slug"], ch["slug"]), md)
        ch["title"] = title
        ch["note"] = str(d.get("note") or "")[:4000]
        ch["draft"] = bool(d.get("draft"))
        if d.get("date"):
            ch["date"] = clean_date(d.get("date"))
        ch["words"] = count_words(md)
        self.save_cfg()
        self.build()
        return {"story": target["slug"], "slug": ch["slug"], "chapter": ch}

    def delete_chapter(self, sslug, cslug):
        s, c = self.get_chapter(sslug, cslug)
        s["chapters"].remove(c)
        self.to_trash(self.chapter_md(s["slug"], c["slug"]))
        self.save_cfg()
        self.build()
        return {"ok": True}

    # ---- settings
    @staticmethod
    def clean_site(d, current):
        out = dict(current)
        for k, limit in (("title", 120), ("tagline", 200), ("footer", 400), ("author", 120), ("lang", 12),
                         ("description", 320)):
            if k in d:
                out[k] = str(d[k]).strip()[:limit]
        if "url" in d:
            out["url"] = str(d["url"]).strip().rstrip("/")[:300]
        if d.get("prose") in ("indented", "spaced"):
            out["prose"] = d["prose"]
        if "wpm" in d:
            try:
                out["wpm"] = max(60, min(1000, int(d["wpm"])))
            except (TypeError, ValueError):
                pass
        for k in SITE_BOOLS:
            if k in d:
                out[k] = bool(d[k])
        out["title"] = out.get("title") or current.get("title") or "My stories"
        out["lang"] = out.get("lang") or "en"
        return out

    def save_settings(self, d):
        site = self.clean_site(d, self.cfg["site"])
        url = site.get("url", "")
        if url and not re.match(r"^https?://[^\s/]+", url):
            raise ApiError("The site URL must start with http:// or https://, for example https://stories.example.com")
        if site.get("rss") and not url:
            raise ApiError("Enter the site URL to turn on the feed. Feed readers need full web addresses.")
        if site.get("rss") and (self.root / "rss").is_dir():
            raise ApiError("A folder named rss already exists here. Rename or remove it to publish the feed.")
        if site.get("sitemap") and not url:
            raise ApiError("Enter the site URL to generate a sitemap. It needs full web addresses.")
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
        css = p.get("css") if isinstance(p.get("css"), str) else read_text(self.css_path())
        view = p.get("view")
        md = str(p.get("markdown") or "")

        if view == "home":
            return {"html": self.render_library(cfg, md, css=css, base="/site/")}

        if view == "page":
            page = next((x for x in cfg["pages"] if x["slug"] == str(p.get("slug") or "")), None)
            if not page:
                raise ApiError("That page does not exist.", 404)
            page["title"] = str(p.get("title") or "").strip() or page["title"]
            page["description"] = str(p.get("description") or "").strip()
            return {"html": self.render_page(cfg, page, md, css=css, base="/site/")}

        if view == "story":
            s = self.find_story(str(p.get("slug") or ""), cfg)
            if not s:
                raise ApiError("That story does not exist.", 404)
            for k in ("title", "subtitle", "author", "blurb", "cover", "warnings", "series", "series_no"):
                if k in p:
                    s[k] = str(p.get(k) or "").strip()
            if p.get("status") in STATUS_LABELS:
                s["status"] = p["status"]
            if p.get("kind") in ("single", "chaptered"):
                s["kind"] = p["kind"] if not (p["kind"] == "single" and s["chapters"]) else s["kind"]
            if "genres" in p:
                s["genres"] = clean_genres(p.get("genres"))
            s["draft"] = False
            if s["kind"] == "single":
                s["words"] = count_words(md)
            return {"html": self.render_story(cfg, s, md, css=css, base="/site/%s/" % s["slug"]),
                    "words": count_words(md)}

        if view == "chapter":
            target = self.find_story(str(p.get("story") or ""), cfg)
            if not target:
                raise ApiError("Create a story first. Chapters live inside stories.", 409)
            ostory = self.find_story(str(p.get("orig_story") or ""), cfg)
            orig, pos = None, None
            if ostory:
                orig = next((c for c in ostory["chapters"] if c["slug"] == str(p.get("orig_slug") or "")), None)
                if orig:
                    pos = ostory["chapters"].index(orig) if ostory is target else None
                    ostory["chapters"].remove(orig)
            ch = dict(orig or {})
            ch.update(
                slug=slugify(str(p.get("new_slug") or "")) or slugify(str(p.get("title") or "")) or "chapter",
                title=str(p.get("title") or "").strip() or "Untitled chapter",
                note=str(p.get("note") or ""), draft=False,
                date=clean_date(p.get("date") or ch.get("date")), words=count_words(md),
            )
            if pos is None:
                target["chapters"].append(ch)
            else:
                target["chapters"].insert(pos, ch)
            if target["kind"] == "single":
                target["kind"] = "chaptered"
            return {"html": self.render_chapter(cfg, target, ch, md, css=css, base="/site/%s/" % target["slug"]),
                    "words": ch["words"]}

        # css and settings preview an existing page
        target = str(p.get("target") or "home")
        kind, _, rest = target.partition(":")
        if kind == "story":
            s = self.find_story(rest, cfg)
            if s:
                return {"html": self.render_story(cfg, s, read_text(self.story_md(s["slug"])), css=css,
                                                  base="/site/%s/" % s["slug"])}
        if kind == "chapter":
            sslug, _, cslug = rest.partition("/")
            s = self.find_story(sslug, cfg)
            ch = next((c for c in s["chapters"] if c["slug"] == cslug), None) if s else None
            if ch:
                return {"html": self.render_chapter(cfg, s, ch, read_text(self.chapter_md(sslug, cslug)), css=css,
                                                    base="/site/%s/" % sslug)}
        if kind == "page":
            page = next((x for x in cfg["pages"] if x["slug"] == rest), None)
            if page:
                return {"html": self.render_page(cfg, page, read_text(self.page_md(page["slug"])), css=css,
                                                 base="/site/")}
        return {"html": self.render_library(cfg, read_text(self.home_md()), css=css, base="/site/")}


# ----------------------------------------------------------------------------
# Opening libraries, remembering the last one
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
    is_lib = (root / META_DIR / "config.json").exists()
    if not is_lib and root.exists() and not confirm:
        if any(not n.name.startswith(".") for n in root.iterdir()):
            return {
                "needs_confirm": True,
                "message": (
                    "This folder already has files that StoryGen did not create.\n\n"
                    "StoryGen will add its own files here and will overwrite index.html and any page "
                    "it generates with the same name. Existing style.css and images are kept.\n\n"
                    "Use this folder anyway?"
                ),
            }
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ApiError("Could not create that folder: %s" % e)
    site = Library(root)
    site.load()
    CURRENT["site"] = site
    remember_folder(root)
    return {"state": site.state()}


def app_state():
    site = CURRENT["site"]
    if site is None:
        return {"folder": None, "suggest": str(Path.home() / "my-stories"), "last": last_folder()}
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

    w = CURRENT["site"]
    if w is None:
        raise ApiError("Open a library folder first.", 409)

    if parts == ["home"]:
        if method == "GET":
            return w.get_home()
        if method == "PUT":
            return w.save_home(h.read_json())
    if parts == ["pages"] and method == "POST":
        return w.add_page(h.read_json().get("title", ""))
    if len(parts) == 2 and parts[0] == "pages":
        if method == "GET":
            return w.get_page(parts[1])
        if method == "PUT":
            return w.save_page(parts[1], h.read_json())
        if method == "DELETE":
            return w.delete_page(parts[1])
    if parts == ["stories"] and method == "POST":
        d = h.read_json()
        return w.add_story(d.get("title", ""), d.get("kind", "chaptered"))
    if len(parts) == 2 and parts[0] == "stories":
        if method == "GET":
            return w.get_story_api(parts[1])
        if method == "PUT":
            return w.save_story(parts[1], h.read_json())
        if method == "DELETE":
            return w.delete_story(parts[1])
    if len(parts) == 3 and parts[0] == "stories" and method == "POST":
        if parts[2] == "move":
            return w.move_story(parts[1], h.read_json().get("dir", 1))
        if parts[2] == "order":
            return w.order_chapters(parts[1], h.read_json().get("chapters", []))
    if parts == ["chapters"] and method == "POST":
        return w.save_chapter(h.read_json())
    if len(parts) == 3 and parts[0] == "chapters":
        if method == "GET":
            return w.get_chapter_api(parts[1], parts[2])
        if method == "DELETE":
            return w.delete_chapter(parts[1], parts[2])
    if parts == ["css"]:
        if method == "GET":
            return w.get_css()
        if method == "PUT":
            return w.save_css(h.read_json().get("css", ""))
    if parts == ["settings"] and method == "PUT":
        return w.save_settings(h.read_json())
    if parts == ["preview"] and method == "POST":
        return w.preview(h.read_json())
    if parts == ["upload"] and method == "POST":
        return w.upload(q.get("name", ["image"])[0], h.read_body(MAX_UPLOAD))
    if parts == ["rescan"] and method == "POST":
        return w.api_rescan()
    if parts == ["build"] and method == "POST":
        w.build()
        return {"ok": True}
    raise ApiError("Unknown request.", 404)


class Handler(BaseHTTPRequestHandler):
    server_version = APP

    def log_message(self, *args):
        pass

    # ---- helpers
    def read_body(self, limit):
        try:
            n = max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            raise ApiError("Bad Content-Length header.")
        if n > limit:
            raise ApiError("That file is too large.", 413)
        return self.rfile.read(n) if n else b""

    def read_json(self):
        raw = self.read_body(8 * 1024 * 1024)
        try:
            d = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            raise ApiError("Bad request body.")
        if not isinstance(d, dict):
            raise ApiError("Bad request body.")
        return d

    def send_bytes(self, status, ctype, data, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
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
        w = CURRENT["site"]
        if w is None:
            return self.send_text(404, "No library is open.")
        rel = unquote(rel)
        if "\x00" in rel:
            return self.send_text(404, "Not found")
        target = (w.root / rel).resolve()
        try:
            parts = target.relative_to(w.root).parts
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
        if ctype.startswith("text/") or ctype.endswith("xml"):
            ctype += "; charset=utf-8"
        # Built pages share an origin with the editor, so scripts are refused outright. The generated
        # site has none, and this stops an uploaded SVG or pasted HTML from reaching the editor API.
        self.send_bytes(200, ctype, target.read_bytes(), {
            "Content-Security-Policy": "script-src 'none'; object-src 'none'; base-uri 'self'",
            "X-Content-Type-Options": "nosniff",
        })

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
# Editor UI (served to your browser; the editor uses JavaScript, generated sites do not)
# ----------------------------------------------------------------------------

UI_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StoryGen</title>
<style>
:root {
  color-scheme: light;
  --ink: #241d18; --ink2: #342a22; --paper: #f1ede6; --panel: #ffffff; --panel2: #f8f5f0; --field: #ffffff;
  --line: #ddd5c8; --text: #24201b; --muted: #6b6459; --accent: #9a5b22;
  --primary: #9a5b22; --primary-d: #7d4917;
  --btn-bg: #ffffff; --btn-hover: #f8f5f0; --btn-line-hover: #bdb2a0;
  --side: #e8e2d8; --side-hover: #ded6c8; --side-active: #ffffff;
  --right: #ddd7cc; --right-bar: #f1ede6;
  --danger: #b3261e; --amber: #8a5a00; --amber-bg: #fff8e6; --amber-line: #e2c98f;
  --ok: #1c7a45;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --ink: #12100e; --ink2: #221d18; --paper: #16130f; --panel: #1d1a16; --panel2: #232019; --field: #14110e;
  --line: #352f27; --text: #e8e2d8; --muted: #a29a8c; --accent: #e0a86a;
  --primary: #a36a2f; --primary-d: #bb7c38;
  --btn-bg: #272219; --btn-hover: #312a20; --btn-line-hover: #574c3a;
  --side: #191612; --side-hover: #241f19; --side-active: #2a241c;
  --right: #100e0c; --right-bar: #17140f;
  --danger: #ff8a80; --amber: #f0c060; --amber-bg: #2b2412; --amber-line: #5a4a1e;
  --ok: #5fd69a;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { height: 100%; margin: 0; }
body { font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--text); background: var(--paper); }
button, input, select, textarea { font: inherit; color: inherit; }
button { border: 1px solid var(--line); background: var(--btn-bg); padding: 5px 11px; border-radius: 6px; cursor: pointer; }
button:hover { border-color: var(--btn-line-hover); background: var(--btn-hover); }
button:disabled { opacity: 0.45; cursor: default; }
button.primary { background: var(--primary); border-color: var(--primary); color: #fff; }
button.primary:hover { background: var(--primary-d); border-color: var(--primary-d); }
button.danger { color: var(--danger); }
input[type=text], input[type=date], input[type=number], input:not([type]), input[type=url], select, textarea.small {
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; background: var(--field); min-width: 0;
}
:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.grow { flex: 1; }
.hint { color: var(--muted); font-size: 12px; }

#workspace { display: flex; flex-direction: column; height: 100vh; }
#top { background: var(--ink); border-bottom: 1px solid var(--ink2); color: #fff; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 8px 12px; }
#top button { padding: 4px 9px; background: transparent; border-color: #4a3f33; color: #f0e9df; }
#top button:hover { background: var(--ink2); border-color: #6b5b49; }
#top .brand { font-weight: 700; margin-right: 6px; }
#top .sep { width: 1px; height: 22px; background: #4a3f33; margin: 0 6px; }
#folder { color: #a89b8b; font: 12px ui-monospace, Consolas, monospace; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#dirty { color: #ffd479; font-size: 12px; }

#main { flex: 1; min-height: 0; display: grid; grid-template-columns: 250px minmax(0, 1fr) minmax(0, 1fr); }
#side { background: var(--side); border-right: 1px solid var(--line); overflow: auto; padding: 8px; }
#side h3 { font-size: 12px; font-weight: 600; color: var(--muted); margin: 14px 6px 4px; }
.item { display: flex; justify-content: space-between; align-items: center; gap: 6px; width: 100%; text-align: left; border: 0; background: transparent; padding: 5px 8px; border-radius: 6px; }
.item:hover { background: var(--side-hover); }
.item.active { background: var(--side-active); box-shadow: inset 3px 0 0 var(--accent); }
.item .t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item.story { font-weight: 600; margin-top: 6px; }
.item.chapter { padding-left: 22px; }
.item.add { padding-left: 22px; color: var(--muted); font-size: 12px; }
.chip { font-size: 11px; padding: 1px 7px; border-radius: 99px; border: 1px solid var(--line); white-space: nowrap; color: var(--muted); }
.chip.draft { color: var(--amber); border-color: var(--amber-line); background: var(--amber-bg); }
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
.md-tools button { padding: 2px 8px; font-size: 13px; }
.code { flex: 1; width: 100%; min-height: 240px; border: 0; resize: none; padding: 14px 16px; background: var(--panel); color: var(--text);
  font: 15px/1.6 ui-monospace, "SF Mono", Consolas, "DejaVu Sans Mono", monospace; tab-size: 2; }
.slug-wrap { display: inline-flex; align-items: center; gap: 2px; color: var(--muted); }
.slug-wrap input { width: 165px; }
.pad { padding: 14px; display: flex; flex-direction: column; gap: 12px; max-width: 620px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field input, .field select, .field textarea { width: 100%; }
.field textarea, textarea.small { border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; background: var(--field); resize: vertical; font: inherit; }
.two { display: flex; gap: 10px; flex-wrap: wrap; }
.two > * { flex: 1 1 12rem; }
fieldset { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
legend { font-weight: 600; padding: 0 4px; }
details.box { border-bottom: 1px solid var(--line); }
details.box > summary { cursor: pointer; padding: 8px 10px; font-weight: 600; background: var(--panel2); list-style: none; }
details.box > summary::-webkit-details-marker { display: none; }
details.box > summary::before { content: "\25B8 "; color: var(--muted); }
details.box[open] > summary::before { content: "\25BE "; }
details.box .pad { max-width: none; }
.ch-row { display: flex; align-items: center; gap: 6px; padding: 5px 10px; border-top: 1px solid var(--line); }
.ch-row .t { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ch-row .n { color: var(--muted); font-variant-numeric: tabular-nums; min-width: 1.5em; text-align: right; }
.ch-row button { padding: 1px 8px; }
.words { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }

#right { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--right); }
#right .bar { background: var(--right-bar); }
#right a { color: var(--accent); }
#pv-wrap { flex: 1; position: relative; overflow: hidden; }
#preview { position: absolute; top: 0; left: 0; border: 0; background: #fff; transform-origin: 0 0; }
#pv-size button { padding: 2px 8px; font-size: 12px; }
#pv-size button.on { background: var(--primary); border-color: var(--primary); color: #fff; }
#toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: var(--ink); color: #fff; padding: 8px 16px; border-radius: 8px; opacity: 0; pointer-events: none; transition: opacity 0.15s; max-width: 80vw; }
#toast.show { opacity: 1; }
#toast.bad { background: #b3261e; }

#welcome { max-width: 540px; margin: 12vh auto; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 28px; }
#welcome h1 { margin: 0 0 6px; font-size: 22px; }
#welcome p { color: var(--muted); margin: 0 0 18px; }
#welcome .row { display: flex; gap: 8px; margin-bottom: 12px; }
#welcome .row input { flex: 1; }
</style>
<script>
(function () {
  var t = null;
  try { t = localStorage.getItem("storygen-theme"); } catch (e) { t = null; }
  if (t !== "dark" && t !== "light") t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", t);
})();
</script>
</head>
<body>

<div id="welcome" hidden>
  <h1>StoryGen</h1>
  <p>Choose the folder where your stories live. If the folder is new or empty, a fresh library is created there. If it holds a StoryGen library, it opens as you left it.</p>
  <div class="row">
    <input id="w-path" aria-label="Library folder path" placeholder="/home/you/my-stories">
    <button id="w-browse">Browse</button>
  </div>
  <button id="w-open" class="primary">Open library</button>
</div>

<div id="workspace" hidden>
  <div id="top">
    <span class="brand">StoryGen</span>
    <button data-act="new-story">New story</button>
    <button data-act="new-chapter">New chapter</button>
    <button data-act="home">Library page</button>
    <button data-act="new-page">Add page</button>
    <span class="sep"></span>
    <button data-act="css">Edit CSS</button>
    <button data-act="settings">Settings</button>
    <button data-act="rescan" title="Pick up .md files and folders added under .storygen/stories">Rescan files</button>
    <span class="grow"></span>
    <span id="dirty" hidden>Unsaved changes</span>
    <span id="folder" title=""></span>
    <button data-act="theme" id="b-theme">Dark mode</button>
    <button data-act="switch">Switch folder</button>
    <button data-act="quit">Quit</button>
  </div>
  <div id="main">
    <aside id="side" aria-label="Stories and chapters"></aside>
    <section id="left" aria-label="Editor"></section>
    <section id="right" aria-label="Preview">
      <div class="bar"><span class="hint">Live preview</span><span id="pv-size" role="group" aria-label="Preview width"><button data-pv="desktop">Desktop</button> <button data-pv="mobile">Mobile</button> <button data-pv="fit">Fit</button></span><span class="grow"></span><a id="open-built" href="/site/index.html" target="_blank" rel="noopener">Open built site in a tab</a></div>
      <div id="pv-wrap"><iframe id="preview" title="Site preview" sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"></iframe></div>
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
let timer = null;
let slugTouched = false;

const MD_TOOLS = `
<div class="md-tools">
  <button data-md="italic" title="Italic"><i>I</i></button>
  <button data-md="bold" title="Bold"><b>B</b></button>
  <button data-md="break" title="Scene break">Scene break</button>
  <button data-md="heading">Heading</button>
  <button data-md="quote" title="Epigraph or quoted passage">Quote</button>
  <button data-md="link">Link</button>
  <button data-md="image">Image</button>
  <span class="grow"></span>
  <span class="words" id="wc"></span>
  <input type="file" id="img-file" accept="image/*" hidden>
</div>`;

const HOME_TPL = `
<div class="fill">
  <div class="bar"><strong>Library page</strong><span class="hint">Your stories are listed below this text.</span><span class="grow"></span><button id="h-save" class="primary">Save</button></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="A short introduction, or leave it empty"></textarea>
</div>`;

const PAGE_TPL = `
<div class="fill">
  <div class="bar">
    <input id="g-title" class="title-input" placeholder="Page title" aria-label="Page title">
    <button id="g-save" class="primary">Save</button>
  </div>
  <div class="bar sub">
    <label>Address <span class="slug-wrap"><input id="g-slug" aria-label="Page address">.html</span></label>
    <span class="grow"></span>
    <button id="g-delete" class="danger">Delete page</button>
  </div>
  <div class="bar sub"><label class="wide">Description <input id="g-desc" maxlength="320" placeholder="Optional, used by search engines"></label></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Write the page in Markdown"></textarea>
</div>`;

const STORY_TPL = `
<div class="fill">
  <div class="bar">
    <input id="s-title" class="title-input" placeholder="Story title" aria-label="Story title">
    <label title="Drafts are saved but left out of the built site"><input type="checkbox" id="s-draft"> Draft</label>
    <button id="s-save" class="primary">Save</button>
  </div>
  <div class="bar sub">
    <label>Address <span class="slug-wrap"><input id="s-slug" aria-label="Story address">/</span></label>
    <label>Status <select id="s-status"></select></label>
    <label>Form <select id="s-kind"><option value="chaptered">Chapters</option><option value="single">Single piece</option></select></label>
    <span class="grow"></span>
    <button id="s-up">Move up</button>
    <button id="s-down">Move down</button>
    <button id="s-delete" class="danger">Delete</button>
  </div>
  <details class="box" id="s-details">
    <summary>Story details: blurb, cover, genres, author, series, dates</summary>
    <div class="pad">
      <div class="two">
        <label class="field">Subtitle <input id="s-subtitle" placeholder="Optional"></label>
        <label class="field">Author <input id="s-author" placeholder="Defaults to the name in Settings"></label>
      </div>
      <label class="field">Blurb <textarea id="s-blurb" rows="3" maxlength="600" placeholder="The pitch, shown on the library page and the story page"></textarea></label>
      <div class="two">
        <label class="field">Cover image <span style="display:flex;gap:6px"><input id="s-cover" placeholder="assets/cover.jpg"><button id="s-cover-pick" type="button">Upload</button></span></label>
        <label class="field">Genres <input id="s-genres" placeholder="Horror, Novelette (separate with commas)"></label>
      </div>
      <label class="field">Content notes <input id="s-warnings" maxlength="400" placeholder="Optional, shown under the blurb"></label>
      <div class="two">
        <label class="field">Series <input id="s-series" placeholder="Optional"></label>
        <label class="field">Number in series <input id="s-series_no" placeholder="2"></label>
      </div>
      <div class="two">
        <label class="field">Started <input type="date" id="s-started"></label>
        <label class="field">Completed <input type="date" id="s-completed"></label>
      </div>
      <label class="field">Meta description <input id="s-description" maxlength="320" placeholder="Defaults to the blurb"></label>
      <input type="file" id="cover-file" accept="image/*" hidden>
    </div>
  </details>
  <details class="box" open id="s-chapbox">
    <summary>Chapters, in reading order</summary>
    <div id="s-chapters"></div>
    <div class="bar"><button id="s-new">New chapter</button><span class="hint" id="s-total"></span></div>
  </details>
  <div class="bar sub"><span class="hint" id="s-md-hint"></span></div>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true"></textarea>
</div>`;

const CHAP_TPL = `
<div class="fill">
  <div class="bar">
    <input id="c-title" class="title-input" placeholder="Chapter title" aria-label="Chapter title">
    <label title="Drafts are saved but left out of the built site"><input type="checkbox" id="c-draft"> Draft</label>
    <button id="c-save" class="primary">Save</button>
  </div>
  <div class="bar sub">
    <label>Story <select id="c-story" aria-label="Story"></select></label>
    <label>Date <input type="date" id="c-date"></label>
    <label>Address <span class="slug-wrap"><span id="c-storyslug"></span>/<input id="c-slug" aria-label="Chapter address">.html</span></label>
    <span class="grow"></span>
    <button id="c-import" title="Load a Markdown or text file from your computer">Import file</button>
    <input type="file" id="c-file" accept=".md,.markdown,.txt,text/markdown,text/plain" hidden>
    <button id="c-up">Move up</button>
    <button id="c-down">Move down</button>
    <button id="c-delete" class="danger">Delete</button>
  </div>
  <details class="box" id="c-notebox">
    <summary>Author's note, shown after the chapter</summary>
    <div class="pad"><textarea id="c-note" class="small" rows="3" placeholder="Optional. Markdown works here too."></textarea></div>
  </details>
  MDTOOLS
  <textarea id="md" class="code" spellcheck="true" placeholder="Write the chapter here. A blank line starts a new paragraph, and *** makes a scene break."></textarea>
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
    <button id="jump-global">Go to theme</button>
    <button id="jump-pages">Go to page specific</button>
  </div>
  <textarea id="css" class="code" spellcheck="false" wrap="off"></textarea>
</div>`;

const SET_TPL = `
<div class="fill">
  <div class="bar"><strong>Settings</strong><span class="grow"></span><button id="st-save" class="primary">Save settings</button></div>
  <div class="pad">
    <label class="field">Site title <input id="st-title"></label>
    <label class="field">Tagline <input id="st-tagline" placeholder="Shown next to the title"></label>
    <label class="field">Author name <input id="st-author" placeholder="Used for bylines and {author} in the footer"></label>
    <label class="field">Meta description <textarea id="st-description" rows="2" maxlength="320" placeholder="Used for the library page in search engines"></textarea></label>
    <label class="field">Footer text <input id="st-footer"><span class="hint">You can use {year}, {title} and {author}. Inline markdown works.</span></label>
    <label class="field">Language code <input id="st-lang" placeholder="en" style="max-width:100px"></label>
    <label class="field">Site URL <input id="st-url" placeholder="https://stories.example.com"><span class="hint">Needed for the feed, canonical links and the sitemap.</span></label>
    <fieldset>
      <legend>Reading</legend>
      <label><input type="radio" name="st-prose" value="indented"> Indented paragraphs, like a printed book</label>
      <label><input type="radio" name="st-prose" value="spaced"> Spaced paragraphs, like a web page</label>
      <label><input type="checkbox" id="st-dropcap"> Drop capital on the first paragraph</label>
      <label><input type="checkbox" id="st-wordcount"> Show word counts</label>
      <label><input type="checkbox" id="st-reading_time"> Show estimated reading time</label>
      <label class="field">Reading speed <input type="number" id="st-wpm" min="60" max="1000" step="10" style="max-width:120px"><span class="hint">Words per minute used for that estimate. 240 is typical for fiction.</span></label>
      <label><input type="checkbox" id="st-updates"> List the latest chapters on the library page</label>
    </fieldset>
    <fieldset>
      <legend>Feed and search engines</legend>
      <label><input type="checkbox" id="st-rss"> Publish a feed of new chapters at <b id="st-rss-addr"></b></label>
      <label><input type="checkbox" id="st-sitemap"> Generate sitemap.xml and robots.txt</label>
      <label><input type="checkbox" id="st-noindex"> Ask search engines not to index this site</label>
    </fieldset>
  </div>
</div>`;

async function api(method, path, body) {
  const opt = { method, headers: { "X-Token": TOKEN } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
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
  toast.t = setTimeout(() => { t.className = ""; }, 3400);
}

function setDirty(v) { dirty = v; $("#dirty").hidden = !v; }
function markDirty() { setDirty(true); countWords(); schedulePreview(); }
async function guard() { return !dirty || window.confirm("You have unsaved changes. Discard them?"); }
function slugify(s) {
  return s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}
const enc = encodeURIComponent;
function story(slug) { return S.stories.find((s) => s.slug === slug); }
function fmt(n) { return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ","); }

function countWords() {
  const el = $("#wc"), ta = $("#md");
  if (!el || !ta) return;
  const n = (ta.value.replace(/[#*_>`\[\]()~-]/g, " ").match(/\S+/g) || []).length;
  el.textContent = fmt(n) + " words";
}

/* ---------- preview ---------- */
let pvMode = "desktop";
try { pvMode = localStorage.getItem("storygen-preview") || "desktop"; } catch (e) { /* ignore */ }

function fitPreview() {
  const wrap = $("#pv-wrap"), f = $("#preview");
  const W = wrap.clientWidth, H = wrap.clientHeight;
  if (!W || !H) return;
  const target = pvMode === "desktop" ? 1280 : pvMode === "mobile" ? 390 : W;
  const s = Math.min(1, W / target);
  f.style.width = target + "px";
  f.style.height = (H / s) + "px";
  f.style.transform = "translateX(" + Math.max(0, (W - target * s) / 2) + "px) scale(" + s + ")";
  $$("#pv-size button").forEach((b) => b.classList.toggle("on", b.dataset.pv === pvMode));
}

$$("#pv-size button").forEach((b) => b.addEventListener("click", () => {
  pvMode = b.dataset.pv;
  try { localStorage.setItem("storygen-preview", pvMode); } catch (e) { /* ignore */ }
  fitPreview();
}));
new ResizeObserver(fitPreview).observe($("#pv-wrap"));

function schedulePreview() { clearTimeout(timer); timer = setTimeout(renderPreview, 280); }

function storyPayload() {
  return {
    view: "story", slug: view.slug, title: $("#s-title").value, subtitle: $("#s-subtitle").value,
    author: $("#s-author").value, blurb: $("#s-blurb").value, cover: $("#s-cover").value,
    genres: $("#s-genres").value, warnings: $("#s-warnings").value, series: $("#s-series").value,
    series_no: $("#s-series_no").value, status: $("#s-status").value, kind: $("#s-kind").value,
    markdown: $("#md").value,
  };
}

function previewPayload() {
  switch (view.type) {
    case "home": return { view: "home", markdown: $("#md").value };
    case "page": return { view: "page", slug: view.slug, title: $("#g-title").value, description: $("#g-desc").value, markdown: $("#md").value };
    case "story": return storyPayload();
    case "chapter": return {
      view: "chapter", story: $("#c-story").value, orig_story: view.story || "", orig_slug: view.slug || "",
      new_slug: $("#c-slug").value, title: $("#c-title").value, date: $("#c-date").value,
      note: $("#c-note").value, markdown: $("#md").value,
    };
    case "css": return { view: "css", target: $("#css-target").value, css: $("#css").value };
    case "settings": return { view: "settings", target: "home", site: readSettings() };
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
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- markdown toolbar ---------- */
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
  else if (kind === "link") wrap("[", "](https://)", "link text");
  else if (kind === "heading") prefix("## ");
  else if (kind === "quote") prefix("> ");
  else if (kind === "break") {
    const before = ta.value.slice(0, s);
    const lead = before && !before.endsWith("\n\n") ? (before.endsWith("\n") ? "\n" : "\n\n") : "";
    ta.setRangeText(lead + "* * *\n\n", s, e, "end");
  }
  ta.dispatchEvent(new Event("input"));
  ta.focus();
}

async function uploadImage(file) {
  const r = await fetch("/api/upload?name=" + enc(file.name), { method: "POST", headers: { "X-Token": TOKEN }, body: file });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "Upload failed");
  return d.path;
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
    if (!f) return;
    try {
      const path = await uploadImage(f);
      ta.setRangeText("![" + f.name.replace(/\.[^.]+$/, "") + "](" + path + ")", ta.selectionStart, ta.selectionEnd, "end");
      ta.dispatchEvent(new Event("input"));
      toast("Image added to assets");
    } catch (e) { toast(e.message, "bad"); }
  });
  countWords();
}

/* ---------- sidebar ---------- */
function renderTop() { $("#folder").textContent = S.folder; $("#folder").title = S.folder; }

function renderSide() {
  const side = $("#side");
  side.innerHTML = "";
  const item = (label, cls, active, fn, chip) => {
    const b = document.createElement("button");
    b.className = "item " + cls + (active ? " active" : "");
    const t = document.createElement("span"); t.className = "t"; t.textContent = label; b.appendChild(t);
    if (chip) { const c = document.createElement("span"); c.className = "chip " + (chip[1] || ""); c.textContent = chip[0]; b.appendChild(c); }
    b.addEventListener("click", fn);
    side.appendChild(b);
  };
  const h = (t) => { const e = document.createElement("h3"); e.textContent = t; side.appendChild(e); };
  item("Library page", "", view.type === "home", () => go(openHome));
  S.pages.forEach((p) => item(p.title, "", view.type === "page" && view.slug === p.slug, () => go(() => openPage(p.slug))));
  h("Stories");
  if (!S.stories.length) {
    const e = document.createElement("div"); e.className = "empty"; e.textContent = "No stories yet. Use New story in the top bar."; side.appendChild(e);
  }
  S.stories.forEach((s) => {
    const chip = s.draft ? ["Draft", "draft"] : (s.kind === "single" ? ["Single"] : [String(s.chapters.length)]);
    item(s.title, "story", view.type === "story" && view.slug === s.slug, () => go(() => openStory(s.slug)), chip);
    s.chapters.forEach((c) => item(c.title, "chapter", view.type === "chapter" && view.story === s.slug && view.slug === c.slug,
      () => go(() => openChapter(s.slug, c.slug)), c.draft ? ["Draft", "draft"] : null));
    if (s.kind !== "single") item("+ New chapter", "add", false, () => go(() => openChapter(null, null, s.slug)));
  });
}

async function refresh() { S = await api("GET", "state"); renderTop(); renderSide(); }
async function go(fn) {
  if (!(await guard())) return;
  setDirty(false);
  try { await fn(); } catch (e) { toast(e.message, "bad"); }
}

/* ---------- library page ---------- */
async function openHome() {
  const d = await api("GET", "home");
  view = { type: "home" };
  $("#left").innerHTML = HOME_TPL.replace("MDTOOLS", MD_TOOLS);
  $("#md").value = d.markdown;
  $("#h-save").addEventListener("click", saveHome);
  bindEditor();
  renderSide();
  renderPreview();
}

async function saveHome() {
  try { await api("PUT", "home", { markdown: $("#md").value }); setDirty(false); toast("Saved"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- extra pages ---------- */
async function newPage() {
  const title = window.prompt("Page title, for example About or Contact");
  if (!title || !title.trim()) return;
  const r = await api("POST", "pages", { title: title.trim() });
  await refresh();
  await openPage(r.slug);
}

async function openPage(slug) {
  const d = await api("GET", "pages/" + enc(slug));
  view = { type: "page", slug };
  $("#left").innerHTML = PAGE_TPL.replace("MDTOOLS", MD_TOOLS);
  $("#g-title").value = d.page.title;
  $("#g-slug").value = d.page.slug;
  $("#g-desc").value = d.page.description || "";
  $("#md").value = d.markdown;
  ["#g-title", "#g-slug", "#g-desc"].forEach((id) => $(id).addEventListener("input", markDirty));
  $("#g-save").addEventListener("click", savePage);
  $("#g-delete").addEventListener("click", deletePage);
  bindEditor();
  renderSide();
  renderPreview();
}

async function savePage() {
  try {
    const r = await api("PUT", "pages/" + enc(view.slug), { title: $("#g-title").value, new_slug: $("#g-slug").value, description: $("#g-desc").value, markdown: $("#md").value });
    view.slug = r.slug;
    $("#g-slug").value = r.slug;
    setDirty(false);
    await refresh();
    toast("Saved");
  } catch (e) { toast(e.message, "bad"); }
}

async function deletePage() {
  if (!window.confirm("Delete this page? The markdown file is moved to .storygen/trash.")) return;
  try {
    await api("DELETE", "pages/" + enc(view.slug));
    setDirty(false);
    await refresh();
    await openHome();
    toast("Page deleted");
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- stories ---------- */
async function newStory() {
  const title = window.prompt("Story title");
  if (!title || !title.trim()) return;
  const kind = window.confirm("Is this a single piece, with no chapters?\n\nOK for a single short story, Cancel for something with chapters.") ? "single" : "chaptered";
  const r = await api("POST", "stories", { title: title.trim(), kind });
  await refresh();
  await openStory(r.slug);
  toast("Story created");
}

async function openStory(slug) {
  const d = await api("GET", "stories/" + enc(slug));
  const s = d.story;
  view = { type: "story", slug };
  $("#left").innerHTML = STORY_TPL.replace("MDTOOLS", MD_TOOLS);
  const st = $("#s-status");
  S.statuses.forEach((x) => { const o = document.createElement("option"); o.value = x.id; o.textContent = x.label; st.appendChild(o); });
  st.value = s.status;
  $("#s-kind").value = s.kind;
  $("#s-title").value = s.title;
  $("#s-slug").value = s.slug;
  $("#s-draft").checked = !!s.draft;
  ["subtitle", "author", "blurb", "cover", "warnings", "series", "series_no", "description"].forEach((k) => { $("#s-" + k).value = s[k] || ""; });
  $("#s-genres").value = (s.genres || []).join(", ");
  $("#s-started").value = s.started || "";
  $("#s-completed").value = s.completed || "";
  $("#md").value = d.markdown;
  $$("#left input, #left textarea, #left select").forEach((i) => {
    if (i.id === "md" || i.type === "file") return;
    i.addEventListener(i.type === "checkbox" ? "change" : "input", markDirty);
  });
  $("#s-kind").addEventListener("change", syncStoryForm);
  $("#s-save").addEventListener("click", saveStory);
  $("#s-delete").addEventListener("click", deleteStory);
  $("#s-up").addEventListener("click", () => moveStory(-1));
  $("#s-down").addEventListener("click", () => moveStory(1));
  $("#s-new").addEventListener("click", () => go(() => openChapter(null, null, view.slug)));
  $("#s-cover-pick").addEventListener("click", () => $("#cover-file").click());
  $("#cover-file").addEventListener("change", async (ev) => {
    const f = ev.target.files[0];
    ev.target.value = "";
    if (!f) return;
    try { $("#s-cover").value = await uploadImage(f); markDirty(); toast("Cover uploaded"); }
    catch (e) { toast(e.message, "bad"); }
  });
  if (!s.blurb && !s.cover) $("#s-details").open = true;
  syncStoryForm();
  renderChapterRows();
  bindEditor();
  renderSide();
  renderPreview();
}

function syncStoryForm() {
  const single = $("#s-kind").value === "single";
  $("#s-chapbox").hidden = single;
  $("#s-md-hint").textContent = single
    ? "The story itself. It appears on the story page under the blurb."
    : "Optional foreword, shown on the story page above the chapter list.";
  $("#md").placeholder = single ? "Write the story here" : "Optional foreword";
  const i = S.stories.findIndex((x) => x.slug === view.slug);
  $("#s-up").disabled = i <= 0;
  $("#s-down").disabled = i < 0 || i >= S.stories.length - 1;
}

function renderChapterRows() {
  const box = $("#s-chapters");
  const s = story(view.slug);
  box.innerHTML = "";
  if (!s || !s.chapters.length) {
    const e = document.createElement("div"); e.className = "empty"; e.textContent = "No chapters yet.";
    box.appendChild(e);
    $("#s-total").textContent = "";
    return;
  }
  let total = 0;
  s.chapters.forEach((c, i) => {
    total += c.words || 0;
    const row = document.createElement("div"); row.className = "ch-row";
    const n = document.createElement("span"); n.className = "n"; n.textContent = String(i + 1); row.appendChild(n);
    const t = document.createElement("span"); t.className = "t"; t.textContent = c.title + (c.draft ? " (draft)" : ""); row.appendChild(t);
    const w = document.createElement("span"); w.className = "words"; w.textContent = c.words ? fmt(c.words) : ""; row.appendChild(w);
    const mk = (label, dis, fn) => { const b = document.createElement("button"); b.textContent = label; b.disabled = dis; b.addEventListener("click", fn); row.appendChild(b); };
    mk("Up", i === 0, () => reorder(i, i - 1));
    mk("Down", i === s.chapters.length - 1, () => reorder(i, i + 1));
    mk("Edit", false, () => go(() => openChapter(s.slug, c.slug)));
    box.appendChild(row);
  });
  $("#s-total").textContent = fmt(total) + " words across " + s.chapters.length + " chapter(s)";
}

async function saveOrder(slug, order) {
  await api("POST", "stories/" + enc(slug) + "/order", { chapters: order });
  await refresh();
  if (view.type === "story") renderChapterRows();
  schedulePreview();
}

async function reorder(i, j) {
  const list = story(view.slug).chapters.map((c) => c.slug);
  list.splice(j, 0, list.splice(i, 1)[0]);
  try { await saveOrder(view.slug, list); } catch (e) { toast(e.message, "bad"); }
}

async function saveStory() {
  const body = storyPayload();
  delete body.view;
  body.new_slug = $("#s-slug").value;
  body.draft = $("#s-draft").checked;
  body.description = $("#s-description").value;
  body.started = $("#s-started").value;
  body.completed = $("#s-completed").value;
  try {
    const r = await api("PUT", "stories/" + enc(view.slug), body);
    view.slug = r.slug;
    $("#s-slug").value = r.slug;
    setDirty(false);
    await refresh();
    renderChapterRows();
    syncStoryForm();
    toast("Saved");
  } catch (e) { toast(e.message, "bad"); }
}

async function moveStory(dir) {
  try { await api("POST", "stories/" + enc(view.slug) + "/move", { dir }); await refresh(); syncStoryForm(); schedulePreview(); }
  catch (e) { toast(e.message, "bad"); }
}

async function deleteStory() {
  const s = story(view.slug);
  const n = s ? s.chapters.length : 0;
  if (!window.confirm("Delete \"" + (s ? s.title : view.slug) + "\"" + (n ? " and its " + n + " chapter(s)" : "") + "?\n\nThe markdown files are moved to .storygen/trash, not destroyed.")) return;
  try {
    await api("DELETE", "stories/" + enc(view.slug));
    setDirty(false);
    await refresh();
    await openHome();
    toast("Story deleted");
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------- chapters ---------- */
async function openChapter(storySlug, slug, forStory) {
  const withChapters = S.stories.filter((s) => s.kind !== "single");
  if (!withChapters.length) {
    toast("Create a story with chapters first.", "bad");
    return;
  }
  let ch = { title: "", slug: "", draft: false, note: "", date: "" }, markdown = "", where = forStory || storySlug;
  if (slug) {
    const d = await api("GET", "chapters/" + enc(storySlug) + "/" + enc(slug));
    ch = d.chapter; markdown = d.markdown; where = d.story;
  }
  if (!where || !withChapters.some((s) => s.slug === where)) {
    where = (view.type === "story" && story(view.slug) && story(view.slug).kind !== "single" && view.slug)
      || (view.type === "chapter" && view.story) || withChapters[0].slug;
  }
  view = { type: "chapter", story: slug ? where : "", slug: slug || "" };
  $("#left").innerHTML = CHAP_TPL.replace("MDTOOLS", MD_TOOLS);
  const sel = $("#c-story");
  withChapters.forEach((s) => { const o = document.createElement("option"); o.value = s.slug; o.textContent = s.title; sel.appendChild(o); });
  sel.value = where;
  $("#c-storyslug").textContent = where;
  $("#c-title").value = ch.title;
  $("#c-slug").value = ch.slug;
  $("#c-date").value = ch.date || new Date().toLocaleDateString("en-CA");
  $("#c-draft").checked = !!ch.draft;
  $("#c-note").value = ch.note || "";
  $("#md").value = markdown;
  if (ch.note) $("#c-notebox").open = true;
  slugTouched = !!slug;
  $("#c-title").addEventListener("input", () => { if (!slugTouched) $("#c-slug").value = slugify($("#c-title").value); markDirty(); });
  $("#c-slug").addEventListener("input", () => { slugTouched = true; markDirty(); });
  ["#c-date", "#c-note"].forEach((id) => $(id).addEventListener("input", markDirty));
  $("#c-draft").addEventListener("change", markDirty);
  sel.addEventListener("change", () => { $("#c-storyslug").textContent = sel.value; markDirty(); });
  $("#c-save").addEventListener("click", saveChapter);
  $("#c-delete").addEventListener("click", deleteChapter);
  $("#c-up").addEventListener("click", () => moveChapter(-1));
  $("#c-down").addEventListener("click", () => moveChapter(1));
  $("#c-import").addEventListener("click", () => $("#c-file").click());
  $("#c-file").addEventListener("change", importFile);
  syncChapterButtons();
  bindEditor();
  renderSide();
  renderPreview();
  if (!slug) $("#c-title").focus();
}

function syncChapterButtons() {
  const saved = !!view.slug;
  $("#c-delete").hidden = !saved;
  $("#c-up").hidden = $("#c-down").hidden = !saved;
  if (!saved) return;
  const s = story(view.story);
  const i = s ? s.chapters.findIndex((c) => c.slug === view.slug) : -1;
  $("#c-up").disabled = i <= 0;
  $("#c-down").disabled = i < 0 || i >= s.chapters.length - 1;
}

function importFile(ev) {
  const f = ev.target.files[0];
  ev.target.value = "";
  if (!f) return;
  const ta = $("#md");
  if (ta.value.trim() && !window.confirm("Replace the text in the editor with " + f.name + "?")) return;
  const rd = new FileReader();
  rd.onload = () => {
    const text = String(rd.result).replace(/\r\n?/g, "\n");
    ta.value = text;
    if (!$("#c-title").value.trim()) {
      const first = text.split("\n").find((l) => l.trim()) || "";
      const m = first.match(/^\s*#\s+(.+?)\s*#*\s*$/);
      const title = m ? m[1] : f.name.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ");
      $("#c-title").value = title.charAt(0).toUpperCase() + title.slice(1);
      if (!slugTouched) $("#c-slug").value = slugify($("#c-title").value);
    }
    markDirty();
    toast("Imported " + f.name + ". Save to keep it.");
  };
  rd.onerror = () => toast("Could not read that file.", "bad");
  rd.readAsText(f);
}

async function saveChapter() {
  const body = {
    story: $("#c-story").value, orig_story: view.story || "", orig_slug: view.slug || "",
    new_slug: $("#c-slug").value, title: $("#c-title").value, date: $("#c-date").value,
    note: $("#c-note").value, draft: $("#c-draft").checked, markdown: $("#md").value,
  };
  try {
    const r = await api("POST", "chapters", body);
    view.story = r.story; view.slug = r.slug;
    $("#c-slug").value = r.slug;
    $("#c-storyslug").textContent = r.story;
    slugTouched = true;
    setDirty(false);
    await refresh();
    syncChapterButtons();
    toast(r.chapter.draft ? "Saved as draft" : "Saved, " + fmt(r.chapter.words) + " words");
  } catch (e) { toast(e.message, "bad"); }
}

async function moveChapter(dir) {
  const s = story(view.story);
  const list = s.chapters.map((c) => c.slug);
  const i = list.indexOf(view.slug), j = i + dir;
  if (i < 0 || j < 0 || j >= list.length) return;
  list.splice(j, 0, list.splice(i, 1)[0]);
  try { await saveOrder(view.story, list); syncChapterButtons(); } catch (e) { toast(e.message, "bad"); }
}

async function deleteChapter() {
  if (!window.confirm("Delete this chapter? The markdown file is moved to .storygen/trash.")) return;
  try {
    const where = view.story;
    await api("DELETE", "chapters/" + enc(view.story) + "/" + enc(view.slug));
    setDirty(false);
    await refresh();
    await openStory(where);
    toast("Chapter deleted");
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
  const opt = (v, label) => { const o = document.createElement("option"); o.value = v; o.textContent = label; tg.appendChild(o); };
  opt("home", "Library page");
  S.pages.forEach((p) => opt("page:" + p.slug, "Page: " + p.title));
  S.stories.forEach((s) => {
    opt("story:" + s.slug, "Story: " + s.title);
    s.chapters.forEach((c) => opt("chapter:" + s.slug + "/" + c.slug, "   " + s.title + ": " + c.title));
  });
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
  if (!window.confirm("Replace the theme variables at the top of your CSS with \"" + t.label + "\"?\n\nEdits inside that top :root block are overwritten. Everything else stays.")) return;
  const re = /:root\s*\{[^}]*\}/;
  ta.value = re.test(ta.value) ? ta.value.replace(re, () => t.block) : t.block + "\n\n" + ta.value;
  markDirty();
}

function jumpTo(marker) {
  const ta = $("#css");
  const i = ta.value.indexOf(marker);
  if (i < 0) { toast("That section marker is not in your CSS any more.", "bad"); return; }
  const lh = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  ta.focus();
  ta.setSelectionRange(i, i);
  ta.scrollTop = Math.max(0, (ta.value.slice(0, i).split("\n").length - 2) * lh);
}

async function saveCss() {
  try { await api("PUT", "css", { css: $("#css").value }); setDirty(false); toast("Saved"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- settings ---------- */
const BOOLS = ["dropcap", "wordcount", "reading_time", "updates", "rss", "sitemap", "noindex"];
function readSettings() {
  const o = {
    title: $("#st-title").value, tagline: $("#st-tagline").value, author: $("#st-author").value,
    description: $("#st-description").value, footer: $("#st-footer").value, lang: $("#st-lang").value,
    url: $("#st-url").value, wpm: parseInt($("#st-wpm").value, 10) || 240,
    prose: ($("input[name=st-prose]:checked") || { value: "indented" }).value,
  };
  BOOLS.forEach((k) => { o[k] = $("#st-" + k).checked; });
  return o;
}

async function openSettings() {
  view = { type: "settings" };
  $("#left").innerHTML = SET_TPL;
  const s = S.site;
  ["title", "tagline", "author", "description", "footer", "lang", "url", "wpm"].forEach((k) => { $("#st-" + k).value = s[k] || ""; });
  BOOLS.forEach((k) => { $("#st-" + k).checked = !!s[k]; });
  const r = $("input[name=st-prose][value=" + (s.prose || "indented") + "]");
  if (r) r.checked = true;
  const addr = () => { $("#st-rss-addr").textContent = (($("#st-url").value.trim().replace(/\/+$/, "")) || "your-site-url") + "/rss"; };
  addr();
  $$("#left input, #left textarea").forEach((i) => i.addEventListener(i.type === "checkbox" || i.type === "radio" ? "change" : "input", () => { addr(); markDirty(); }));
  $("#st-save").addEventListener("click", saveSettings);
  renderSide();
  renderPreview();
}

async function saveSettings() {
  try { await api("PUT", "settings", readSettings()); setDirty(false); await refresh(); toast("Saved"); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------- top bar ---------- */
function themeLabel() {
  $("#b-theme").textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "Light mode" : "Dark mode";
}

function toggleTheme() {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("storygen-theme", next); } catch (e) { /* ignore */ }
  themeLabel();
}

async function rescan() {
  const r = await api("POST", "rescan");
  await refresh();
  const bits = [];
  if (r.stories_added) bits.push(r.stories_added + " story/stories added");
  if (r.chapters_added) bits.push(r.chapters_added + " chapter(s) added");
  if (r.chapters_removed) bits.push(r.chapters_removed + " chapter(s) removed because their file is gone");
  toast(bits.length ? bits.join(", ") : "No new files found. Site rebuilt.");
  schedulePreview();
}

async function act(name) {
  if (name === "theme") { toggleTheme(); return; }
  if (name === "quit") {
    if (!(await guard())) return;
    await api("POST", "quit");
    document.body.innerHTML = "<p style='padding:40px;font:16px system-ui'>StoryGen has stopped. You can close this tab.</p>";
    return;
  }
  await go(async () => {
    if (name === "new-story") await newStory();
    else if (name === "new-chapter") await openChapter(null, null, null);
    else if (name === "new-page") await newPage();
    else if (name === "home") await openHome();
    else if (name === "css") await openCss();
    else if (name === "settings") await openSettings();
    else if (name === "rescan") await rescan();
    else if (name === "switch") await chooseFolder();
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
  if (!r.available) path = window.prompt("Folder path for your library", (S && S.folder) || (inputEl && inputEl.value) || "");
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
  await openHome();
}

$$("#top [data-act]").forEach((b) => b.addEventListener("click", () => act(b.dataset.act)));

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (view.type === "home") saveHome();
    else if (view.type === "page") savePage();
    else if (view.type === "story") saveStory();
    else if (view.type === "chapter") saveChapter();
    else if (view.type === "css") saveCss();
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
    ap = argparse.ArgumentParser(description="StoryGen: local fiction site generator with a markdown editor and live preview.")
    ap.add_argument("folder", nargs="?", help="library folder to open (created if it does not exist)")
    ap.add_argument("--port", type=int, default=8767, help="port for the local editor (default 8767)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab automatically")
    args = ap.parse_args()

    if args.folder:
        try:
            r = open_site(args.folder)
            if r.get("needs_confirm"):
                print("That folder has files StoryGen did not create. Open it from the editor to confirm.")
        except ApiError as e:
            print("Could not open folder:", e)
    else:
        last = last_folder()
        if last and (Path(last) / META_DIR / "config.json").exists():
            try:
                open_site(last)
            except Exception as e:  # noqa: BLE001
                print("Could not reopen the last library:", e)

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
    print("StoryGen is running at %s (Ctrl+C to stop)" % url)
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
